/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The hero case: a value-retaining 4-cycle mule, which is the case the 4-minute
   demo script opens (plan §15). Two other cases exist so the queue's deep links
   resolve, and an account with no counterparties in the window so the
   window-empty state is reachable from a real route rather than only the gallery.

   THESE ARE THE SERVED BYTES. `GET /api/cases/{case_id}` answers
   `Envelope[CaseDetail]` (`apps/api/routers/cases.py:73-81`), and the workspace shape the
   page consumes is derived from it by `lib/api/contract.ts:deriveCasePayload`. The double
   therefore hand-writes `CaseDetail` and every nested model it names, and lets the same
   decoder run over it — a pre-derived fixture could keep agreeing with the client while
   disagreeing with the server, which is the failure this file exists to make impossible.

   The three cards are deliberately three different calibration states: two
   `calibrated_band` readings with their rate and population, and one `uncalibrated` block
   carrying no rate, no band, no population and the fold's own reason — the state plan 03
   §H and DEV-024 require the case to state rather than to print as a number. */

import type { ServedCaseDetailWire as ServedCaseDetail } from '../lib/api/contract';
import { money, seeded } from './common.fixture';

const HERO = 'ACC-7F2A19';
const RUN_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2D';
const CALIBRATION_NOTE =
  'the validation fold held 34 positives against a floor of 500, so no rate was measured for this band and calibration was refused';

function transactions(count: number, accountKey: string): ServedCaseDetail['transactions'] {
  const next = seeded(88);
  const out: ServedCaseDetail['transactions'] = [];
  for (let index = 0; index < count; index += 1) {
    const inbound = index % 3 !== 0;
    const minor = Math.round((inbound ? 4_500_000 : 4_200_000) + next() * 3_000_000);
    const counterparty = `ACC-${(0x210000 + index * 0x1f3).toString(16).toUpperCase().slice(0, 6)}`;
    out.push({
      txn_id: `ibmaml:${String(100_000 + index * 7)}`,
      event_ts_utc: new Date(
        Date.UTC(2026, 8, 11, 3 + Math.floor(index / 4), (index * 17) % 60, (index * 7) % 60),
      ).toISOString(),
      local_hour: (3 + Math.floor(index / 4)) % 24,
      event_date_local: '2026-09-11',
      src_account_key: inbound ? counterparty : accountKey,
      dst_account_key: inbound ? accountKey : counterparty,
      amount: money(minor),
      txn_type: inbound ? 'TRANSFER' : 'CASH_OUT',
      // Balances are served as raw minor-unit integers with no currency attached, exactly
      // as `readmodel` stores them — the double does not dress them in one either.
      src_balance_before: inbound ? null : 12_000_000,
      src_balance_after: inbound ? null : 12_000_000 - minor,
      dst_balance_before: inbound ? 4_000_000 : null,
      dst_balance_after: inbound ? 4_000_000 + minor : null,
      label_fraud: index < 3 ? true : null,
      label_typology: index < 3 ? 'R4' : null,
    });
  }
  return out;
}

/** `evidence_event` rows. `detail` is the producer-chosen bag the route forwards, so the
 *  double puts real scalars in it rather than a pre-rendered sentence. */
function evidence(count: number, accountKey: string): ServedCaseDetail['evidence'] {
  const txns = transactions(count, accountKey);
  const out: ServedCaseDetail['evidence'] = txns.map((txn, index) => ({
    id: index + 1,
    occurred_at: txn.event_ts_utc,
    kind: index % 7 === 0 ? 'rule_hit' : 'transaction',
    label:
      index % 7 === 0
        ? 'R4 CYCLE_MEMBER closed a 4-hop loop'
        : `${txn.txn_type} · ${txn.dst_account_key === accountKey ? 'inbound' : 'outbound'}`,
    txn_id: txn.txn_id,
    rule_id: index % 7 === 0 ? 'R4' : null,
    object_key: null,
    detail:
      index % 7 === 0
        ? { hop_count: 4, retention: 0.71, retention_floor: 0.6 }
        : { amount_minor: txn.amount.minor, direction: txn.dst_account_key === accountKey ? 'in' : 'out' },
  }));
  out.unshift({
    id: 0,
    occurred_at: new Date(Date.UTC(2026, 8, 11, 0, 0, 0)).toISOString(),
    kind: 'window',
    label: 'Scoring window opened',
    txn_id: null,
    rule_id: null,
    object_key: 'exposure-window/2026-09-11',
    detail: { window_hours: 24, downstream_hops: 1 },
  });
  return out;
}

function decisions(
  caseId: string,
  rows: readonly {
    seq: number;
    action: 'escalate' | 'dismiss' | 'review' | 'reverse';
    reason: string;
    actor: string;
    roles: string[];
    at: string;
    exposureMinor: number;
    fourEyes: boolean;
    fourEyesState: 'not_required' | 'pending' | 'confirmed';
    reverses?: string | null;
    superseded?: boolean;
  }[],
): ServedCaseDetail['decision_history'] {
  let prevHash = '0'.repeat(64);
  return rows.map((row, index) => {
    const hash = `${row.seq.toString(16).repeat(4)}${prevHash.slice(0, 60)}`;
    const record = {
      decision_id: `01J4Z7D${caseId.slice(6, 15)}S${String(row.seq).padStart(10, '0')}`.slice(0, 64),
      decision_seq: row.seq,
      chain_seq: index + 1,
      action: row.action,
      reason: row.reason,
      actor_id: row.actor,
      actor_roles: row.roles,
      occurred_at: row.at,
      exposure: money(row.exposureMinor),
      four_eyes_required: row.fourEyes,
      four_eyes_state: row.fourEyesState,
      confirmed_by: row.fourEyesState === 'confirmed' ? 'reviewer.nabirye' : null,
      confirmed_at: row.fourEyesState === 'confirmed' ? row.at : null,
      reversal_of_decision_id: row.reverses ?? null,
      decided_on_superseded_run: row.superseded ?? false,
      prev_hash: prevHash,
      row_hash: hash,
    };
    prevHash = hash;
    return record;
  });
}

/** `scorecard_point` rows, in the served order (`points` ascending, `routers/cases.py:112`).
 *  `reason_code` copies the attribute key — the id, kept twice — because that is what the
 *  landing job stores, and the human sentence stays in the scoring artefact. */
const heroPoints: ServedCaseDetail['scorecard_points'] = [
  {
    attribute: 'account_age_days',
    bin_label: '>=720',
    points: 34,
    woe: -1.042_118,
    reason_code: 'account_age_days',
    population_share: 0.28,
    bad_rate: 0.01,
  },
  {
    attribute: 'median_txn_amount',
    bin_label: 'missing',
    points: -6,
    woe: 0.318_402,
    reason_code: 'median_txn_amount',
    population_share: 0.061,
    bad_rate: 0.11,
  },
  {
    attribute: 'cycle_retention_score',
    bin_label: '0.60|0.85',
    points: -27,
    woe: 1.402_977,
    reason_code: 'cycle_retention_score',
    population_share: 0.018,
    bad_rate: 0.39,
  },
  {
    attribute: 'distinct_senders_24h',
    bin_label: '>=11',
    points: -31,
    woe: 1.618_403,
    reason_code: 'distinct_senders_24h',
    population_share: 0.031,
    bad_rate: 0.44,
  },
  {
    attribute: 'pass_through_ratio_1h',
    bin_label: '0.86|1.00',
    points: -48,
    woe: 2.331_045,
    reason_code: 'pass_through_ratio_1h',
    population_share: 0.012,
    bad_rate: 0.63,
  },
];

/** `reason_codes` as the current artefact stores them: rendered sentences. The alerts
 *  router documents the second stored shape (`{code,label,points}` maps); the hero card
 *  carries sentences and the quiet card carries maps, so both branches of the client's
 *  reader are exercised by fixture mode rather than only claimed. */
const heroReasonCodes: ServedCaseDetail['reason_codes'] = [
  {
    code: 'pass_through_ratio_1h',
    label: 'Pass-through ratio in top decile: minus 48 points',
    points: -48,
  },
  {
    code: 'distinct_senders_24h',
    label: 'Distinct inbound counterparties above the ninth decile: minus 31 points',
    points: -31,
  },
  { code: 'cycle_retention_score', label: 'Member of a value-retaining cycle: minus 27 points', points: -27 },
];

const heroShap: ServedCaseDetail['shap'] = [
  {
    feature: 'pass_through_ratio_1h',
    shap: -0.31,
    feature_value: 0.94,
    rank: 1,
    evidence_txn_ids: ['ibmaml:100000', 'ibmaml:100007', 'ibmaml:100014'],
  },
  {
    feature: 'distinct_senders_24h',
    shap: -0.18,
    feature_value: 13,
    rank: 2,
    evidence_txn_ids: ['ibmaml:100021', 'ibmaml:100028', 'ibmaml:100035', 'ibmaml:100042'],
  },
  {
    feature: 'cycle_retention_score',
    shap: -0.14,
    feature_value: 0.71,
    rank: 3,
    evidence_txn_ids: ['ibmaml:100049', 'ibmaml:100056'],
  },
  { feature: 'account_age_days', shap: 0.09, feature_value: 940, rank: 4, evidence_txn_ids: [] },
];

const heroRuleHits: ServedCaseDetail['rule_hits'] = [
  {
    rule_id: 'R4',
    rule_name: 'CYCLE_MEMBER',
    typology: 'R4',
    fired: true,
    observed: 0.71,
    threshold: 0.6,
    detail: { hop_count: 4, retention_floor: 0.6, max_length: 6 },
  },
  {
    rule_id: 'R1',
    rule_name: 'RAPID_PASS_THROUGH',
    typology: 'R1',
    fired: true,
    observed: 0.94,
    threshold: 0.8,
    detail: { window_minutes: 60, min_inflow_minor: 10_000 },
  },
  {
    rule_id: 'R10',
    rule_name: 'FAST_CASH_OUT',
    typology: 'R10',
    fired: false,
    observed: 0.61,
    threshold: 0.7,
    detail: { holding_hours: 6 },
  },
];

const heroEconomics: ServedCaseDetail['economics'] = {
  exposure: money(412_000_000),
  expected_value: money(98_050_000),
  loss_avoided: money(144_200_000),
  analyst_minutes: 120,
  analyst_cost: money(1_800_000),
  friction_cost: money(2_500_000),
  recovery_rate: 0.35,
  recovery_sensitivity_band: [0.2, 0.35, 0.5],
  assumptions: [
    {
      key: 'recovery.rate',
      value: 0.35,
      source: 'economics row (run copy) / config key recovery.rate',
      note: 'stored with the run so a later edit of economics.yaml cannot reinterpret this money',
    },
    {
      key: 'analyst.cost_per_minute_minor',
      value: 15_000,
      source: 'economics row (run copy) / config key analyst.cost_per_minute_minor',
      note: 'stored with the run so a later edit of economics.yaml cannot reinterpret this money',
    },
    {
      key: 'friction_cost_minor',
      value: 2_500_000,
      source: 'economics row (run copy) / config key friction_cost_minor',
      note: 'stored with the run so a later edit of economics.yaml cannot reinterpret this money',
    },
  ],
  monte_carlo: {
    runs: 10_000,
    seed: 1337,
    p05: money(190_000_000),
    p50: money(312_000_000),
    p95: money(440_000_000),
    interval: [0.05, 0.95],
  },
};

/** The route's own `counterfactual()` output for the hero card: the cheapest single
 *  stored-bin change that crosses a band line, in integer points. */
const heroCounterfactual: ServedCaseDetail['counterfactual'] = {
  possible: true,
  current_band: 'E',
  current_action: 'escalate with the subgraph attached',
  total_points: 1_246,
  cheapest_change: {
    attribute: 'pass_through_ratio_1h',
    from_bin: '0.86|1.00',
    to_bin: '0.60|0.86',
    points_delta: 26,
    resulting_points: 1_272,
    resulting_band: 'D',
    resulting_action: 'review this period',
  },
  alternatives: [
    {
      attribute: 'distinct_senders_24h',
      from_bin: '>=11',
      to_bin: '7|11',
      points_delta: 19,
      resulting_points: 1_265,
      resulting_band: 'D',
      resulting_action: 'review this period',
    },
  ],
  basis: 'stored scorecard_point vs scorecard_bin vs band_definition; integer points, no model called',
};

const heroWatchlist: ServedCaseDetail['watchlist'] = {
  list_name: 'internal-adverse-media',
  list_version: '2026-09-01',
  record_count: 18_402,
  hits: [{ matched_field: 'counterparty_name', score: 0.81, list_record: 'IAD-04412' }],
  advisory_only: true,
  note: 'Screening matched one counterparty name. An empty hit list means nothing was matched against, not that the account is clean, and no decision follows from this block automatically.',
};

function servedCase(
  overrides: Partial<ServedCaseDetail> & Pick<ServedCaseDetail, 'case_id' | 'account_key'>,
): ServedCaseDetail {
  return {
    pinned_run_id: RUN_ID,
    run_state: 'complete',
    superseded: false,
    band: 'E',
    fused_score: 0.912,
    scorecard_points_total: 1_246,
    scorecard_points: heroPoints,
    calibration: {
      kind: 'calibrated_band',
      band: '0.90-1.00',
      observed_rate: 0.71,
      n: 432,
      note: null,
    },
    predicted_typology: 'R4',
    model_version: 'lgbm-fusion-4.5.3+isotonic',
    reason_codes: heroReasonCodes,
    rule_ids: ['R1', 'R4', 'R10'],
    economics: heroEconomics,
    evidence: evidence(28, overrides.account_key),
    transactions: transactions(28, overrides.account_key),
    transaction_total: 28,
    shap: heroShap,
    rule_hits: heroRuleHits,
    watchlist: heroWatchlist,
    decision_history: [],
    case_version: 3,
    status: 'decided',
    rank_under_active_policy: 3,
    counterfactual: heroCounterfactual,
    ...overrides,
  };
}

export const HERO_CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2E';
export const QUIET_CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2F';
export const ISOLATED_CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2G';

export const heroCase: ServedCaseDetail = servedCase({
  case_id: HERO_CASE_ID,
  account_key: HERO,
  decision_history: decisions(HERO_CASE_ID, [
    {
      seq: 1,
      action: 'review',
      reason:
        'Pulled the account onto the desk: four-hop loop with retention above the floor, and the cash-out leg is inside the window.',
      actor: 'analyst.okello',
      roles: ['analyst'],
      at: new Date(Date.UTC(2026, 8, 24, 9, 41, 12)).toISOString(),
      exposureMinor: 412_000_000,
      fourEyes: false,
      fourEyesState: 'not_required',
    },
    {
      seq: 2,
      action: 'escalate',
      reason:
        'Counterparty set repeats across three other flagged accounts; escalating with the subgraph attached rather than closing on the account alone.',
      actor: 'reviewer.nabirye',
      roles: ['reviewer', 'supervisor'],
      at: new Date(Date.UTC(2026, 8, 24, 11, 6, 38)).toISOString(),
      exposureMinor: 412_000_000,
      fourEyes: true,
      fourEyesState: 'confirmed',
      reverses: '01J4Z7D2QK9N7V1C4X6E8GS000000000001',
    },
  ]),
});

/** A second, lower-band case so deep links from the queue all resolve. Its reason codes
 *  arrive in the OTHER stored shape — `{code,label,points}` maps with no points on one row —
 *  so the client's two-branch reader is exercised here, and a null points value stays null
 *  rather than being printed as zero. */
export const quietCase: ServedCaseDetail = servedCase({
  case_id: QUIET_CASE_ID,
  account_key: 'ACC-4B1C07',
  band: 'B',
  fused_score: 0.318,
  scorecard_points_total: 612,
  rank_under_active_policy: 148,
  calibration: { kind: 'calibrated_band', band: '0.30-0.40', observed_rate: 0.281, n: 168, note: null },
  predicted_typology: 'R6',
  reason_codes: [
    { code: 'distinct_senders_24h', label: 'Distinct inbound counterparties above the ninth decile', points: -31 },
    { code: 'cycle_retention_score', label: 'Member of a value-retaining cycle', points: -27 },
    { code: 'night_volume_share', label: 'Quiet-hour volume share in the top decile', points: null },
  ],
  decision_history: [],
  // A case with no writes is at version 1, not 0: `version=1` when the case is opened
  // (`apps/api/decisions.py`), and `expected_version` is `Field(ge=1)`.
  case_version: 1,
  status: 'open',
  watchlist: null,
  counterfactual: {
    possible: false,
    current_band: 'B',
    total_points: 612,
    note: 'no single stored bin change moves this account across a band boundary; the band is a consequence of several attributes at once, which is the point of a scorecard and not a gap in this response',
  },
});

/** The window-empty case, scored but with no counterparties in the drawn window — and
 *  `uncalibrated`: the fold fell under the calibration floor, so there is no rate, no band
 *  and no population here, only the run's reason. It is the case that proves the rail
 *  renders a labelled state rather than a missing number. */
export const isolatedCase: ServedCaseDetail = servedCase({
  case_id: ISOLATED_CASE_ID,
  account_key: 'ACC-00DORM',
  band: 'A',
  fused_score: 0.041,
  scorecard_points_total: 288,
  rank_under_active_policy: null,
  calibration: { kind: 'uncalibrated', band: null, observed_rate: null, n: null, note: CALIBRATION_NOTE },
  predicted_typology: null,
  reason_codes: [],
  rule_ids: [],
  evidence: [],
  transactions: [],
  transaction_total: 0,
  shap: [],
  rule_hits: [],
  decision_history: [],
  case_version: 1,
  status: 'open',
  counterfactual: null,
});

export const CASE_ID_BY_ACCOUNT_KEY: Record<string, string> = {
  [HERO]: HERO_CASE_ID,
  'ACC-4B1C07': QUIET_CASE_ID,
  'ACC-00DORM': ISOLATED_CASE_ID,
};

export const CASES: Record<string, ServedCaseDetail> = {
  [HERO]: heroCase,
  'ACC-4B1C07': quietCase,
  'ACC-00DORM': isolatedCase,
};
