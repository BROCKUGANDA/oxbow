/* =============================================================================
   OXBOW client contract — the typed shape of every response the web app reads.

   WHY THIS FILE IS THE ONLY PLACE A SHAPE IS DECLARED.
   Plan §14's hardest gate is "no route renders a value that is not in the API
   response". That is not enforceable at the component layer, so it is enforced
   here instead: every field a component may read is declared once, with a runtime
   decoder, and the fetch layer will not hand a component an object that did not
   pass its decoder. A field the server stops sending is a decode failure at the
   boundary — visible, typed, carrying the response path — rather than `undefined`
   rendered as a blank or, worse, defaulted to a number nobody asked the API for.

   ENVELOPE. Operational payloads are `{ data, meta }` and nothing else. There is
   no `success` boolean anywhere in the product; status lives in the HTTP code, and
   `unwrap()` in client.ts is the single place the envelope is opened. Components
   never see it. `packages`/`rows`/`items` keys are not accepted, so a route that
   drifts away from the envelope fails loudly at the boundary.

   SEAMS TO RECONCILE WITH P7 (plan §13, routes not yet built as of this session).
   Each is marked `SEAM(P7)` on its own line and listed in `SEAMS` at the bottom of
   the file, which is what a reconciliation pass reads. Nothing here invents a
   field silently: where P7's schema already exists on disk
   (`apps/api/schemas/common.py`, `apps/api/problems.py`) this file mirrors it.
   ============================================================================= */

import { type Decoder, array, boolean, integer, nullable, number, object, oneOf, record, string } from '../codec';
import { ProblemDetailDecoder } from './problem';

/* ============================================================ shared shapes */

/** A JSON scalar — assumption values and rule-parameter values are a string or a
 *  number and nothing else, so it is decoded as that union rather than widened. */
const scalarOrNumber: Decoder<string | number> = {
  kind: 'scalar',
  decode(value, path) {
    if (typeof value === 'string') return { ok: true, value };
    if (typeof value === 'number' && Number.isFinite(value)) return { ok: true, value };
    return { ok: false, error: { path, message: 'expected a string or number' } };
  },
};

/** `Money` from schemas/common.py: minor units + currency + decimals, always together. */
export type Money = { minor: number; currency: string; decimals: number };
export const MoneyDecoder: Decoder<Money> = object('Money', {
  minor: integer,
  currency: string,
  decimals: integer,
});

/** `AssumptionLine`: one economic assumption a figure in this payload depends on. */
export type AssumptionLine = { key: string; value: string | number; source: string; note: string | null };
export const AssumptionLineDecoder: Decoder<AssumptionLine> = object('AssumptionLine', {
  key: string,
  value: scalarOrNumber,
  source: string,
  note: nullable(string),
});

/** `Meta`: correlation and provenance on every response. */
export type Meta = {
  run_id: string | null;
  trace_id: string | null;
  model_version: string | null;
  /** 'pipeline' | 'fixture' | 'null-adapter' | 'offline-snapshot' — rendered, never assumed. */
  provenance: string | null;
  generated_at: string | null;
  assumptions: AssumptionLine[];
  degraded: boolean;
  degraded_reason: string | null;
  disclaimer: string;
};
export const MetaDecoder: Decoder<Meta> = object('Meta', {
  run_id: nullable(string),
  trace_id: nullable(string),
  model_version: nullable(string),
  provenance: nullable(string),
  generated_at: nullable(string),
  assumptions: array(AssumptionLineDecoder),
  degraded: boolean,
  degraded_reason: nullable(string),
  disclaimer: string,
});

/**
 * The `meta` block as it actually arrives: `Meta` for a detail route, plus the
 * `PageMeta` fields for a list route. P7's envelope forbids any third top-level
 * key, so paging has to live here rather than beside `data`, and the paging
 * fields are optional because a detail route does not send them.
 */
export type PageFields = {
  limit: number | null;
  offset: number | null;
  total: number | null;
  next_offset: number | null;
  sort: string | null;
  order: 'asc' | 'desc' | null;
};

export type ListMeta = Meta & PageFields;

const PageFieldsDecoder: Decoder<PageFields> = object('PageFields', {
  limit: nullable(integer),
  offset: nullable(integer),
  total: nullable(integer),
  next_offset: nullable(integer),
  sort: nullable(string),
  order: nullable(oneOf('order', 'asc', 'desc')),
});

/** The paging half of `meta`, absent on a detail route. */
const NO_PAGE: PageFields = {
  limit: null,
  offset: null,
  total: null,
  next_offset: null,
  sort: null,
  order: null,
};

export const ListMetaDecoder: Decoder<ListMeta> = {
  kind: 'ListMeta',
  decode(value, path) {
    const base = MetaDecoder.decode(value, path);
    if (!base.ok) return base;
    const page = PageFieldsDecoder.decode(value, `${path}.meta`);
    // A detail route simply has no paging fields. That is not a contract failure —
    // `PageMeta` on the server only adds them on list routes — so they default to
    // null and the renderer says "unbounded" rather than inventing a row count.
    return { ok: true, value: { ...base.value, ...(page.ok ? page.value : NO_PAGE) } };
  },
};

/** The risk bands, letters first: the letter and the meter always accompany colour. */
export const BandDecoder = oneOf('Band', 'A', 'B', 'C', 'D', 'E');
export type Band = 'A' | 'B' | 'C' | 'D' | 'E';

/** Rule ids R1-R12, keyed to the twelve glyphs. */
export const TypologyDecoder = oneOf(
  'Typology',
  'R1',
  'R2',
  'R3',
  'R4',
  'R5',
  'R6',
  'R7',
  'R8',
  'R9',
  'R10',
  'R11',
  'R12',
);
export type Typology = 'R1' | 'R2' | 'R3' | 'R4' | 'R5' | 'R6' | 'R7' | 'R8' | 'R9' | 'R10' | 'R11' | 'R12';

/** A currency figure that is legally not allowed to render without its assumptions. */
export type MoneyFigure = { value: Money; band: Money[] | null; band_rates: number[] | null };
export const MoneyFigureDecoder: Decoder<MoneyFigure> = object('MoneyFigure', {
  value: MoneyDecoder,
  band: nullable(array(MoneyDecoder)),
  band_rates: nullable(array(number)),
});

/** A point in a series. Nothing in the UI plots a point that is not one of these. */
export type SeriesPoint = { x: string; y: number; label: string | null };
export const SeriesPointDecoder: Decoder<SeriesPoint> = object('SeriesPoint', {
  x: string,
  y: number,
  label: nullable(string),
});

export const TimestampDecoder = string;

/* ============================================================== runtime meta */

/** `GET /api/meta/run` — the deployment facts every renderer needs. */
export type RuntimeMeta = {
  run_id: string | null;
  deployment_timezone: string;
  currency: string;
  minor_units_per_major: number;
  economics_source: string;
  economics: Record<string, string | number>;
  dataset: string | null;
  licence: string | null;
  model_version: string | null;
  demo_data: boolean;
};
export const RuntimeMetaDecoder: Decoder<RuntimeMeta> = object('RuntimeMeta', {
  run_id: nullable(string),
  deployment_timezone: string,
  currency: string,
  minor_units_per_major: integer,
  economics_source: string,
  economics: record(scalarOrNumber),
  dataset: nullable(string),
  licence: nullable(string),
  model_version: nullable(string),
  demo_data: boolean,
});

/* ============================================================ 1. dashboard */

/** `GET /api/dashboard` — the command strip. Every figure carries its band. */
export type Dashboard = {
  expected_loss_avoided: MoneyFigure;
  benefit_per_analyst_hour: MoneyFigure;
  residual_exposure: MoneyFigure;
  alerts_generated: number;
  high_risk_networks: number;
  capacity: { reviewed: number; available: number; unit: string; period_label: string };
  model_quality: {
    pr_auc: MetricWithDelta;
    precision_at_budget: MetricWithDelta;
    brier: MetricWithDelta;
  };
  cumulative_benefit: {
    series: { label: string; points: SeriesPoint[]; is_policy: boolean }[];
    x_axis_label: string;
    y_axis_label: string;
    y_is_money: boolean;
  };
  band_distribution: { band: Band; accounts: number; population_share: number }[];
  latest_patterns: PatternHit[];
  economics_source: string;
};

export type MetricWithDelta = {
  value: number;
  delta_vs_baseline: number | null;
  baseline_label: string | null;
  unit: string;
  /** Either two bounds or null; `number[]` because a decoder cannot promise a length. */
  ci: number[] | null;
};

const MetricWithDeltaDecoder: Decoder<MetricWithDelta> = object('Metric', {
  value: number,
  delta_vs_baseline: nullable(number),
  baseline_label: nullable(string),
  unit: string,
  ci: nullable(array(number)),
});

export type PatternHit = {
  typology: Typology;
  rule_code: string;
  account_key: string;
  case_href: string;
  severity: number;
  exposure: MoneyFigure;
  observed: string;
  first_seen: string;
  community_id: number | null;
};

const PatternHitDecoder: Decoder<PatternHit> = object('PatternHit', {
  typology: TypologyDecoder,
  rule_code: string,
  account_key: string,
  case_href: string,
  severity: number,
  exposure: MoneyFigureDecoder,
  observed: string,
  first_seen: TimestampDecoder,
  community_id: nullable(integer),
});

const BandDistributionDecoder = object('BandBucket', {
  band: BandDecoder,
  accounts: integer,
  population_share: number,
});

export const DashboardDecoder: Decoder<Dashboard> = object('Dashboard', {
  expected_loss_avoided: MoneyFigureDecoder,
  benefit_per_analyst_hour: MoneyFigureDecoder,
  residual_exposure: MoneyFigureDecoder,
  alerts_generated: integer,
  high_risk_networks: integer,
  capacity: object('Capacity', {
    reviewed: integer,
    available: integer,
    unit: string,
    period_label: string,
  }),
  model_quality: object('ModelQuality', {
    pr_auc: MetricWithDeltaDecoder,
    precision_at_budget: MetricWithDeltaDecoder,
    brier: MetricWithDeltaDecoder,
  }),
  cumulative_benefit: object('CumulativeBenefit', {
    series: array(
      object('Series', {
        label: string,
        points: array(SeriesPointDecoder),
        is_policy: boolean,
      }),
    ),
    x_axis_label: string,
    y_axis_label: string,
    y_is_money: boolean,
  }),
  band_distribution: array(BandDistributionDecoder),
  latest_patterns: array(PatternHitDecoder),
  economics_source: string,
});

/* ============================================================= 2. alert queue */

export type ReasonCode = {
  text: string;
  attribute: string;
  bin: string;
  points: number;
  contribution: number | null;
};

export const ReasonCodeDecoder: Decoder<ReasonCode> = object('ReasonCode', {
  text: string,
  attribute: string,
  bin: string,
  points: number,
  contribution: nullable(number),
});

/** Calibration bin — the confidence label is the observed rate and n, never an adjective. */
export type CalibrationBin = {
  bin_label: string;
  observed_rate: number;
  n: number;
  expected_rate: number;
};
export const CalibrationBinDecoder: Decoder<CalibrationBin> = object('CalibrationBin', {
  bin_label: string,
  observed_rate: number,
  n: integer,
  expected_rate: number,
});

export type AlertRow = {
  account_key: string;
  case_href: string;
  score: number;
  band: Band;
  calibration: CalibrationBin;
  typology: Typology | null;
  reasons: ReasonCode[];
  exposure: MoneyFigure;
  expected_value: MoneyFigure;
  rank: number;
  policy_label: string;
  review_minutes: number;
  /** Server-computed: the rank at which the capacity line falls in this page. */
  above_capacity: boolean;
  scored_at: string;
  txn_count: number;
  first_seen: string;
  last_seen: string;
};

const AlertRowDecoder: Decoder<AlertRow> = object('AlertRow', {
  account_key: string,
  case_href: string,
  score: number,
  band: BandDecoder,
  calibration: CalibrationBinDecoder,
  typology: nullable(TypologyDecoder),
  reasons: array(ReasonCodeDecoder),
  exposure: MoneyFigureDecoder,
  expected_value: MoneyFigureDecoder,
  rank: integer,
  policy_label: string,
  review_minutes: number,
  above_capacity: boolean,
  scored_at: TimestampDecoder,
  txn_count: integer,
  first_seen: TimestampDecoder,
  last_seen: TimestampDecoder,
});

export type QueueCapacity = {
  cutoff_rank: number | null;
  minutes_available: number;
  minutes_committed: number;
  unreviewed_exposure: MoneyFigure | null;
  unreviewed_count: number;
  period_label: string;
  policy_label: string;
};

/**
 * The queue page. `capacity` is what the cutoff line is drawn from: the rank the
 * active policy can review, and the money the unreviewed remainder is worth.
 * The rows are `data.rows`; the paging window is `meta`.
 */
export type QueueFacets = {
  bands: { band: Band; count: number }[];
  typologies: { typology: Typology; rule_code: string; count: number }[];
};

export type QueueFilterRecovery = {
  narrowest: string;
  label: string;
  rows_if_removed: number;
  unfiltered_rows: number;
};

export type AlertPage = {
  rows: AlertRow[];
  capacity: QueueCapacity;
  facets: QueueFacets;
  /** Narrowest-predicate data for the filters-excluded empty state. */
  filter_recovery: QueueFilterRecovery | null;
};

export const AlertPageDecoder: Decoder<AlertPage> = object('AlertPage', {
  rows: array(AlertRowDecoder),
  capacity: object('QueueCapacity', {
    cutoff_rank: nullable(integer),
    minutes_available: number,
    minutes_committed: number,
    unreviewed_exposure: nullable(MoneyFigureDecoder),
    unreviewed_count: integer,
    period_label: string,
    policy_label: string,
  }),
  facets: object('Facets', {
    bands: array(object('BandFacet', { band: BandDecoder, count: integer })),
    typologies: array(object('TypologyFacet', { typology: TypologyDecoder, rule_code: string, count: integer })),
  }),
  filter_recovery: nullable(
    object('FilterRecovery', {
      narrowest: string,
      label: string,
      rows_if_removed: integer,
      unfiltered_rows: integer,
    }),
  ),
});

/* ========================================================== 3. case workspace */

export type ScorecardPoints = {
  attribute: string;
  label: string;
  bin: string;
  points: number;
  population_share: number;
  bad_rate: number;
  is_reason_code: boolean;
};

const ScorecardPointsDecoder: Decoder<ScorecardPoints> = object('ScorecardPoint', {
  attribute: string,
  label: string,
  bin: string,
  points: number,
  population_share: number,
  bad_rate: number,
  is_reason_code: boolean,
});

export type MonteCarloInterval = {
  lower: Money;
  upper: Money;
  runs: number;
  max_depth: number;
  seed: number;
  interval: number[];
};

const MonteCarloIntervalDecoder: Decoder<MonteCarloInterval> = object('MonteCarlo', {
  lower: MoneyDecoder,
  upper: MoneyDecoder,
  runs: integer,
  max_depth: integer,
  seed: integer,
  interval: array(number),
});

export type CaseEconomics = {
  exposure: MoneyFigure;
  expected_value: MoneyFigure;
  review_minutes: number;
  analyst_cost: MoneyFigure;
  friction_cost: Money;
  monte_carlo: MonteCarloInterval | null;
  recovery_rate: number;
};

const CaseEconomicsDecoder: Decoder<CaseEconomics> = object('CaseEconomics', {
  exposure: MoneyFigureDecoder,
  expected_value: MoneyFigureDecoder,
  review_minutes: number,
  analyst_cost: MoneyFigureDecoder,
  friction_cost: MoneyDecoder,
  monte_carlo: nullable(MonteCarloIntervalDecoder),
  recovery_rate: number,
});

export type CaseHeader = {
  account_key: string;
  run_id: string;
  score: number;
  band: Band;
  calibration: CalibrationBin;
  typology: Typology | null;
  points: ScorecardPoints[];
  reasons: ReasonCode[];
  economics: CaseEconomics;
  fusion: {
    p_scorecard: number;
    p_gbm: number;
    p_fused: number;
    coefficients: { input: string; weight: number }[];
  } | null;
  decided_on_superseded_run: boolean;
  model_version: string | null;
  feature_spec_hash: string | null;
  scored_at: string;
};

const CaseHeaderDecoder: Decoder<CaseHeader> = object('CaseHeader', {
  account_key: string,
  run_id: string,
  score: number,
  band: BandDecoder,
  calibration: CalibrationBinDecoder,
  typology: nullable(TypologyDecoder),
  points: array(ScorecardPointsDecoder),
  reasons: array(ReasonCodeDecoder),
  economics: CaseEconomicsDecoder,
  fusion: nullable(
    object('Fusion', {
      p_scorecard: number,
      p_gbm: number,
      p_fused: number,
      coefficients: array(object('Coefficient', { input: string, weight: number })),
    }),
  ),
  decided_on_superseded_run: boolean,
  model_version: nullable(string),
  feature_spec_hash: nullable(string),
  scored_at: TimestampDecoder,
});

export type EvidenceEvent = {
  id: string;
  ts_utc: string;
  kind: 'transaction' | 'rule_hit' | 'window' | 'decision' | 'ingest';
  title: string;
  detail: string;
  amount: Money | null;
  rule_code: string | null;
  typology: Typology | null;
  txn_ids: string[];
};

const EvidenceEventDecoder: Decoder<EvidenceEvent> = object('EvidenceEvent', {
  id: string,
  ts_utc: TimestampDecoder,
  kind: oneOf('EvidenceKind', 'transaction', 'rule_hit', 'window', 'decision', 'ingest'),
  title: string,
  detail: string,
  amount: nullable(MoneyDecoder),
  rule_code: nullable(string),
  typology: nullable(TypologyDecoder),
  txn_ids: array(string),
});

export type TransactionRow = {
  txn_id: string;
  ts_utc: string;
  type: string;
  channel: string;
  amount: Money;
  counterparty_key: string | null;
  direction: 'in' | 'out';
  src_balance_after: Money | null;
  dst_balance_after: Money | null;
  balance_delta: Money | null;
  is_zero_value: boolean;
  rule_codes: string[];
};

const TransactionRowDecoder: Decoder<TransactionRow> = object('TransactionRow', {
  txn_id: string,
  ts_utc: TimestampDecoder,
  type: string,
  channel: string,
  amount: MoneyDecoder,
  counterparty_key: nullable(string),
  direction: oneOf('Direction', 'in', 'out'),
  src_balance_after: nullable(MoneyDecoder),
  dst_balance_after: nullable(MoneyDecoder),
  balance_delta: nullable(MoneyDecoder),
  is_zero_value: boolean,
  rule_codes: array(string),
});

/** SHAP waterfall entry, persisted per row by P4b — the UI never computes one. */
export type Contribution = {
  feature: string;
  label: string;
  sentence: string;
  value: number;
  direction: 'increases' | 'decreases';
  rank: number;
  /** The cross-filter key: clicking this row filters the other panes to these ids. */
  txn_ids: string[];
  evidence_ids: string[];
  source: 'shap' | 'scorecard_points';
};

const ContributionDecoder: Decoder<Contribution> = object('Contribution', {
  feature: string,
  label: string,
  sentence: string,
  value: number,
  direction: oneOf('Direction', 'increases', 'decreases'),
  rank: integer,
  txn_ids: array(string),
  evidence_ids: array(string),
  source: oneOf('ContributionSource', 'shap', 'scorecard_points'),
});

export type RuleHit = {
  rule_code: string;
  typology: Typology;
  name: string;
  severity: number;
  observed: string;
  parameters: { key: string; value: string | number }[];
  overlap_group: string | null;
  counted_once: boolean;
  txn_ids: string[];
  first_hit: string;
  last_hit: string;
};

const RuleHitDecoder: Decoder<RuleHit> = object('RuleHit', {
  rule_code: string,
  typology: TypologyDecoder,
  name: string,
  severity: number,
  observed: string,
  parameters: array(
    object('Parameter', {
      key: string,
      value: scalarOrNumber,
    }),
  ),
  overlap_group: nullable(string),
  counted_once: boolean,
  txn_ids: array(string),
  first_hit: TimestampDecoder,
  last_hit: TimestampDecoder,
});

/** The computed counterfactual: the score with the top contribution removed. */
export type Counterfactual = {
  feature: string;
  label: string;
  score_without: number;
  band_without: Band | null;
  expected_value_without: MoneyFigure;
  statement: string;
};

const CounterfactualDecoder: Decoder<Counterfactual> = object('Counterfactual', {
  feature: string,
  label: string,
  score_without: number,
  band_without: nullable(BandDecoder),
  expected_value_without: MoneyFigureDecoder,
  statement: string,
});

/**
 * The four decision actions the API accepts — `DecisionAction` in
 * `apps/api/schemas/case.py:27`. There is no fifth: a client-side `revisit` reached
 * `DecisionCreate` as an unparseable literal and the whole write died at validation.
 */
export const DecisionActionDecoder = oneOf('DecisionAction', 'escalate', 'dismiss', 'review', 'reverse');
export type DecisionAction = 'escalate' | 'dismiss' | 'review' | 'reverse';

/** `four_eyes_state` — `apps/api/schemas/case.py:188` and `:284`. */
export const FourEyesStateDecoder = oneOf('FourEyesState', 'not_required', 'pending', 'confirmed');
export type FourEyesState = 'not_required' | 'pending' | 'confirmed';

/** One append-only decision, with its chain position. */
export type Decision = {
  seq: number;
  decision: DecisionAction;
  reason: string;
  actor: string;
  role: string;
  recorded_at: string;
  hash: string;
  prev_hash: string | null;
  reversible_of: number | null;
  four_eyes_required: boolean;
  superseded_run: boolean;
  /** Local-only: true while the write is in flight and unconfirmed by the server. */
  pending?: boolean | undefined;
};

const DecisionDecoder: Decoder<Decision> = object('Decision', {
  seq: integer,
  decision: DecisionActionDecoder,
  reason: string,
  actor: string,
  role: string,
  recorded_at: TimestampDecoder,
  hash: string,
  prev_hash: nullable(string),
  reversible_of: nullable(integer),
  four_eyes_required: boolean,
  superseded_run: boolean,
});

export type CasePayload = {
  header: CaseHeader;
  evidence: EvidenceEvent[];
  transactions: TransactionRow[];
  contributions: Contribution[];
  rule_hits: RuleHit[];
  counterfactual: Counterfactual | null;
  decisions: Decision[];
  /** Optimistic-concurrency token for the next write (plan §13: a stale token is a 409). */
  decision_version: number;
  narrative: { text: string; source: 'llm' | 'template'; degraded: boolean } | null;
};

export const CasePayloadDecoder: Decoder<CasePayload> = object('CasePayload', {
  header: CaseHeaderDecoder,
  evidence: array(EvidenceEventDecoder),
  transactions: array(TransactionRowDecoder),
  contributions: array(ContributionDecoder),
  rule_hits: array(RuleHitDecoder),
  counterfactual: nullable(CounterfactualDecoder),
  decisions: array(DecisionDecoder),
  decision_version: integer,
  narrative: nullable(
    object('Narrative', {
      text: string,
      source: oneOf('NarrativeSource', 'llm', 'template'),
      degraded: boolean,
    }),
  ),
});

/**
 * The write body, field for field from `DecisionCreate` in
 * `apps/api/schemas/case.py:237-265`.
 *
 * Two things this deliberately does NOT carry, because the model is `extra="forbid"`
 * and an undeclared key is a 422 rather than an ignored hint:
 * * `decision` — the API's field is `action`;
 * * `idempotency_key` — the server derives the outbox key itself as
 *   `sha256(run_id|case_id|decision_seq)` (`apps/api/routers/decisions.py:234`), so a
 *   client-chosen one would be a second, contradictory claim about the same write.
 */
export type DecisionCreateBody = {
  action: DecisionAction;
  reason: string;
  expected_version: number;
  reversal_of_decision_id: string | null;
};

/**
 * What a successful write returns — `DecisionWriteResult` in
 * `apps/api/schemas/case.py:268-289`, all twelve fields, as `extra="forbid"`.
 *
 * `outbox_queued` plus `four_eyes_state` is the honest four-eyes answer: a decision
 * above the threshold is stored and hash-chained but no delivery is promised until a
 * second, different reviewer confirms it, and the client says exactly that rather
 * than inferring a state from a missing field.
 */
export type DecisionWriteResult = {
  case_id: string;
  decision_id: string;
  decision_seq: number;
  chain_seq: number;
  row_hash: string;
  four_eyes_required: boolean;
  four_eyes_state: FourEyesState;
  outbox_queued: boolean;
  case_version: number;
  decided_on_superseded_run: boolean;
  audit_seq: number | null;
  occurred_at: string;
};

export const DecisionWriteResultDecoder: Decoder<DecisionWriteResult> = object('DecisionWriteResult', {
  case_id: string,
  decision_id: string,
  decision_seq: integer,
  chain_seq: integer,
  row_hash: string,
  four_eyes_required: boolean,
  four_eyes_state: FourEyesStateDecoder,
  outbox_queued: boolean,
  case_version: integer,
  decided_on_superseded_run: boolean,
  audit_seq: nullable(integer),
  occurred_at: TimestampDecoder,
});

/**
 * The second reviewer's body — `FourEyesConfirm` in `apps/api/schemas/case.py:292-296`.
 * The note has the same minimum length as a decision reason, so a confirmation cannot
 * be a click.
 */
export type FourEyesConfirmBody = { expected_version: number; confirmation_note: string };

/* ============================================================ 4. network */

export type GraphNode = {
  key: string;
  band: Band | null;
  exposure: Money | null;
  degree: number;
  community_id: number;
  node_type: 'account' | 'rail' | 'external' | 'meta';
  flagged: boolean;
  is_cycle_member: boolean;
  hops: number;
  true_size: number | null;
};

const GraphNodeDecoder: Decoder<GraphNode> = object('GraphNode', {
  key: string,
  band: nullable(BandDecoder),
  exposure: nullable(MoneyDecoder),
  degree: number,
  community_id: integer,
  node_type: oneOf('NodeType', 'account', 'rail', 'external', 'meta'),
  flagged: boolean,
  is_cycle_member: boolean,
  hops: integer,
  true_size: nullable(integer),
});

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  ts_first: string;
  ts_last: string;
  count: number;
  total: Money;
  typology: Typology | null;
  is_reversal: boolean;
  high_velocity: boolean;
};

const GraphEdgeDecoder: Decoder<GraphEdge> = object('GraphEdge', {
  id: string,
  source: string,
  target: string,
  ts_first: TimestampDecoder,
  ts_last: TimestampDecoder,
  count: number,
  total: MoneyDecoder,
  typology: nullable(TypologyDecoder),
  is_reversal: boolean,
  high_velocity: boolean,
});

export type Subgraph = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  cap: number;
  collapsed_communities: { community_id: number; true_size: number }[];
  window: { from: string; to: string };
  hops: number;
  edges_by_bucket: { bucket: string; edges: number }[];
  overlays: {
    cycles: number;
    high_velocity_hops: number;
    fan_stars: number;
    dense_communities: number;
    flagged: number;
  };
};

export const SubgraphDecoder: Decoder<Subgraph> = object('Subgraph', {
  nodes: array(GraphNodeDecoder),
  edges: array(GraphEdgeDecoder),
  truncated: boolean,
  cap: integer,
  collapsed_communities: array(object('Collapsed', { community_id: integer, true_size: integer })),
  window: object('Window', { from: TimestampDecoder, to: TimestampDecoder }),
  hops: integer,
  edges_by_bucket: array(object('Bucket', { bucket: TimestampDecoder, edges: integer })),
  overlays: object('Overlays', {
    cycles: integer,
    high_velocity_hops: integer,
    fan_stars: integer,
    dense_communities: integer,
    flagged: integer,
  }),
});

/* =========================================================== 5. scorecard */

export type ScorecardAttribute = {
  attribute: string;
  label: string;
  sentence: string;
  iv: number;
  admitted: boolean;
  refusal_reason: string | null;
  bins: {
    bin: string;
    woe: number;
    points: number;
    population_share: number;
    bad_rate: number;
    bad_count: number;
    is_special: 'missing' | 'structural_zero' | 'unseen' | null;
  }[];
};

const ScorecardAttributeDecoder: Decoder<ScorecardAttribute> = object('ScorecardAttribute', {
  attribute: string,
  label: string,
  sentence: string,
  iv: number,
  admitted: boolean,
  refusal_reason: nullable(string),
  bins: array(
    object('Bin', {
      bin: string,
      woe: number,
      points: number,
      population_share: number,
      bad_rate: number,
      bad_count: integer,
      is_special: nullable(oneOf('SpecialBin', 'missing', 'structural_zero', 'unseen')),
    }),
  ),
});

export type Scorecard = {
  scaling: { pdo: number; base_score: number; base_odds: number; factor: number; offset: number; formula: string };
  admission_rule: { min_iv: number; max_iv: number; above_max_action: string; source: string };
  attributes: ScorecardAttribute[];
  bands: {
    band: Band;
    lower: number;
    upper: number;
    population_share: number;
    observed_rate: number;
    action: string;
    accounts: number;
  }[];
  points_total_reconciles: boolean;
  source: string;
};

export const ScorecardDecoder: Decoder<Scorecard> = object('Scorecard', {
  scaling: object('Scaling', {
    pdo: number,
    base_score: number,
    base_odds: number,
    factor: number,
    offset: number,
    formula: string,
  }),
  admission_rule: object('Admission', {
    min_iv: number,
    max_iv: number,
    above_max_action: string,
    source: string,
  }),
  attributes: array(ScorecardAttributeDecoder),
  bands: array(
    object('ScorecardBand', {
      band: BandDecoder,
      lower: number,
      upper: number,
      population_share: number,
      observed_rate: number,
      action: string,
      accounts: integer,
    }),
  ),
  points_total_reconciles: boolean,
  source: string,
});

export type Drift = {
  psi_by_period: { period: string; psi: number; verdict: 'ok' | 'watch' | 'action' }[];
  csi_by_feature: { feature: string; label: string; csi: number }[];
  rating_migration: { from: Band; to: Band; count: number }[];
  downgrade_rate: number;
  thresholds: { watch: number; action: number; source: string };
};

export const DriftDecoder: Decoder<Drift> = object('Drift', {
  psi_by_period: array(
    object('Psi', { period: string, psi: number, verdict: oneOf('Verdict', 'ok', 'watch', 'action') }),
  ),
  csi_by_feature: array(object('Csi', { feature: string, label: string, csi: number })),
  rating_migration: array(object('Migration', { from: BandDecoder, to: BandDecoder, count: integer })),
  downgrade_rate: number,
  thresholds: object('DriftThresholds', { watch: number, action: number, source: string }),
});

export type DisagreementRow = {
  account_key: string;
  case_href: string;
  scorecard_score: number;
  gbm_score: number;
  delta: number;
  band: Band;
  exposure: MoneyFigure;
};

export type DisagreementPage = {
  rows: DisagreementRow[];
  compared_accounts: number;
  max_delta: number;
  near_miss_threshold: number;
  rows_at_threshold: number;
};

const DisagreementRowDecoder: Decoder<DisagreementRow> = object('DisagreementRow', {
  account_key: string,
  case_href: string,
  scorecard_score: number,
  gbm_score: number,
  delta: number,
  band: BandDecoder,
  exposure: MoneyFigureDecoder,
});

export const DisagreementPageDecoder: Decoder<DisagreementPage> = object('DisagreementPage', {
  rows: array(DisagreementRowDecoder),
  compared_accounts: integer,
  max_delta: number,
  near_miss_threshold: number,
  rows_at_threshold: integer,
});

/* ==================================================== 6. policy simulation */

export type AllocationRequest = {
  capacity_minutes: number;
  recovery_rate: number;
  analyst_cost_per_hour: Money;
  friction_cost: Money;
};

export type Allocation = {
  policy_label: string;
  allocator: 'greedy' | 'cpsat' | 'cpsat_timeout_greedy';
  accounts_reviewed: number;
  expected_loss_avoided: MoneyFigure;
  benefit_per_analyst_hour: MoneyFigure;
  customers_wrongly_touched: number;
  optimality_gap: { absolute: Money; ratio: number; cpsat_objective: number; greedy_objective: number } | null;
  max_drawdown: MoneyFigure;
  var95_unreviewed: MoneyFigure;
  es975_unreviewed: MoneyFigure;
  cumulative_curve: { label: string; points: SeriesPoint[]; is_policy: boolean }[];
  frontier: {
    points: { capacity_minutes: number; loss_avoided: Money; wrongly_touched: number }[];
    current_index: number;
  };
  changed: { entered: string[]; left: string[]; entered_count: number; left_count: number };
  solve_ms: number | null;
  degraded: boolean;
};

export const AllocationDecoder: Decoder<Allocation> = object('Allocation', {
  policy_label: string,
  allocator: oneOf('Allocator', 'greedy', 'cpsat', 'cpsat_timeout_greedy'),
  accounts_reviewed: integer,
  expected_loss_avoided: MoneyFigureDecoder,
  benefit_per_analyst_hour: MoneyFigureDecoder,
  customers_wrongly_touched: integer,
  optimality_gap: nullable(
    object('Gap', {
      absolute: MoneyDecoder,
      ratio: number,
      cpsat_objective: number,
      greedy_objective: number,
    }),
  ),
  max_drawdown: MoneyFigureDecoder,
  var95_unreviewed: MoneyFigureDecoder,
  es975_unreviewed: MoneyFigureDecoder,
  cumulative_curve: array(object('Series', { label: string, points: array(SeriesPointDecoder), is_policy: boolean })),
  frontier: object('Frontier', {
    points: array(
      object('FrontierPoint', {
        capacity_minutes: number,
        loss_avoided: MoneyDecoder,
        wrongly_touched: integer,
      }),
    ),
    current_index: integer,
  }),
  changed: object('Changed', {
    entered: array(string),
    left: array(string),
    entered_count: integer,
    left_count: integer,
  }),
  solve_ms: nullable(number),
  degraded: boolean,
});

export type PolicyBaseline = { label: string; points: SeriesPoint[]; is_current: boolean };

export type PolicyDefaults = {
  capacity_minutes: number;
  capacity_bounds: { min: number; max: number; step: number };
  recovery_rate: number;
  recovery_band: number[];
  analyst_cost_per_hour: Money;
  friction_cost: Money;
  currency: string;
  source: string;
  period_label: string;
  baselines: PolicyBaseline[];
};

export const PolicyDefaultsDecoder: Decoder<PolicyDefaults> = object('PolicyDefaults', {
  capacity_minutes: number,
  capacity_bounds: object('CapacityBounds', { min: number, max: number, step: number }),
  recovery_rate: number,
  recovery_band: array(number),
  analyst_cost_per_hour: MoneyDecoder,
  friction_cost: MoneyDecoder,
  currency: string,
  source: string,
  period_label: string,
  baselines: array(
    object('Baseline', {
      label: string,
      points: array(SeriesPointDecoder),
      is_current: boolean,
    }),
  ),
});

/* ============================================== 7. model / backtest / valid */

export type DatasetCard = {
  name: string;
  url: string;
  licence: string;
  licence_note: string;
  citation: string;
  retrieved_at: string;
  files: { filename: string; sha256: string; rows: number; bytes: number }[];
  rows: number;
  period: { from: string; to: string };
  class_balance: { label: string; count: number; rate: number }[];
  splits: { name: string; from: string; to: string; positives: number }[];
  label_definition: string;
  label_limits: string[];
  sampling_rule: string | null;
  known_biases: string[];
};

export const DatasetCardDecoder: Decoder<DatasetCard> = object('DatasetCard', {
  name: string,
  url: string,
  licence: string,
  licence_note: string,
  citation: string,
  retrieved_at: TimestampDecoder,
  files: array(object('FileEntry', { filename: string, sha256: string, rows: integer, bytes: integer })),
  rows: integer,
  period: object('Period', { from: TimestampDecoder, to: TimestampDecoder }),
  class_balance: array(object('Balance', { label: string, count: integer, rate: number })),
  splits: array(object('Split', { name: string, from: TimestampDecoder, to: TimestampDecoder, positives: integer })),
  label_definition: string,
  label_limits: array(string),
  sampling_rule: nullable(string),
  known_biases: array(string),
});

export type Validation = {
  corpora: { key: string; label: string; note: string }[];
  folds: {
    index: number;
    train_from: string;
    train_to: string;
    embargo_days: number;
    test_from: string;
    test_to: string;
    positives: number;
    skipped_reason: string | null;
  }[];
  optimised_on: string;
  entity_disjoint_note: string;
  baseline_table: {
    corpus: string;
    variant: string;
    pr_auc: number;
    pr_auc_ci: number[];
    precision_at_budget: number;
    recall_at_budget: number;
    net_benefit: MoneyFigure;
    benefit_per_analyst_hour: MoneyFigure;
    is_final: boolean;
  }[];
  pr_curve: { corpus: string; recall: number; precision: number }[];
  operating_point: { corpus: string; recall: number; precision: number; budget_label: string };
  reliability: { bin: string; predicted: number; observed: number; n: number }[];
  brier: number;
  calibration_floor: { min_positives: number; refused: boolean; method: string };
  confusion: { tp: number; fp: number; fn: number; tn: number; budget_label: string; precision_undefined: boolean };
  ablation: {
    variant: string;
    question: string;
    pr_auc: number;
    ci: number[];
    net_benefit: MoneyFigure;
    corpus: string;
  }[];
  shap_importance: { feature: string; label: string; mean_abs: number }[];
  typology_recall: { typology: Typology; rule_code: string; recall: number; support: number }[];
  fairness: { dimension: string; bucket: string; false_positive_rate: number; n: number }[];
  perturbations: { name: string; magnitude: number; measure: string; result: number; note: string }[];
  drawdown: { policy: string; value: Money; zero_because: string | null }[];
  risk_adjusted: { value: number; formula: string; not_sharpe_because: string };
  seeds: { count: number; mean: number; sd: number; metric: string };
  configurations_evaluated: number;
  test_touched_at: string | null;
  limitations: string[];
  degraded_dependencies: { name: string; fallback: string }[];
};

export const ValidationDecoder: Decoder<Validation> = object('Validation', {
  corpora: array(object('Corpus', { key: string, label: string, note: string })),
  folds: array(
    object('Fold', {
      index: integer,
      train_from: TimestampDecoder,
      train_to: TimestampDecoder,
      embargo_days: number,
      test_from: TimestampDecoder,
      test_to: TimestampDecoder,
      positives: integer,
      skipped_reason: nullable(string),
    }),
  ),
  optimised_on: string,
  entity_disjoint_note: string,
  baseline_table: array(
    object('BaselineRow', {
      corpus: string,
      variant: string,
      pr_auc: number,
      pr_auc_ci: array(number),
      precision_at_budget: number,
      recall_at_budget: number,
      net_benefit: MoneyFigureDecoder,
      benefit_per_analyst_hour: MoneyFigureDecoder,
      is_final: boolean,
    }),
  ),
  pr_curve: array(object('PrPoint', { corpus: string, recall: number, precision: number })),
  operating_point: object('OperatingPoint', {
    corpus: string,
    recall: number,
    precision: number,
    budget_label: string,
  }),
  reliability: array(object('ReliabilityBin', { bin: string, predicted: number, observed: number, n: integer })),
  brier: number,
  calibration_floor: object('CalibrationFloor', {
    min_positives: number,
    refused: boolean,
    method: string,
  }),
  confusion: object('Confusion', {
    tp: integer,
    fp: integer,
    fn: integer,
    tn: integer,
    budget_label: string,
    precision_undefined: boolean,
  }),
  ablation: array(
    object('AblationRow', {
      variant: string,
      question: string,
      pr_auc: number,
      ci: array(number),
      net_benefit: MoneyFigureDecoder,
      corpus: string,
    }),
  ),
  shap_importance: array(object('ShapImportance', { feature: string, label: string, mean_abs: number })),
  typology_recall: array(
    object('TypologyRecall', { typology: TypologyDecoder, rule_code: string, recall: number, support: integer }),
  ),
  fairness: array(
    object('FairnessBucket', { dimension: string, bucket: string, false_positive_rate: number, n: integer }),
  ),
  perturbations: array(
    object('Perturbation', { name: string, magnitude: number, measure: string, result: number, note: string }),
  ),
  drawdown: array(object('Drawdown', { policy: string, value: MoneyDecoder, zero_because: nullable(string) })),
  risk_adjusted: object('RiskAdjusted', {
    value: number,
    formula: string,
    not_sharpe_because: string,
  }),
  seeds: object('Seeds', { count: integer, mean: number, sd: number, metric: string }),
  configurations_evaluated: integer,
  test_touched_at: nullable(TimestampDecoder),
  limitations: array(string),
  degraded_dependencies: array(object('DegradedDep', { name: string, fallback: string })),
});

/* ==================================================== SSE: the stage ledger */

/** One SSE frame. Mirrors the Pydantic event model in apps/api/events.py. */
export type StageEventPayload = {
  id: string;
  stage: string;
  status: 'pending' | 'running' | 'complete' | 'failed' | 'skipped';
  rows: number | null;
  elapsed_ms: number | null;
  run_id: string;
};

export const StageEventDecoder: Decoder<StageEventPayload> = object('StageEvent', {
  id: string,
  stage: string,
  status: oneOf('StageStatus', 'pending', 'running', 'complete', 'failed', 'skipped'),
  rows: nullable(integer),
  elapsed_ms: nullable(number),
  run_id: string,
});

/* ============================================================ route registry */

export type RouteSpec<T> = {
  readonly id: string;
  readonly path: string;
  readonly data: Decoder<T>;
};

/**
 * Every route the app reads. `data` is the *inside* of the envelope: `unwrap()`
 * applies it to `body.data` and hands back `{ data, meta }`.
 */
export const ROUTES = {
  dashboard: { id: 'dashboard', path: '/api/dashboard', data: DashboardDecoder },
  alerts: { id: 'alerts', path: '/api/alerts', data: AlertPageDecoder },
  case: { id: 'case', path: '/api/cases', data: CasePayloadDecoder },
  subgraph: { id: 'subgraph', path: '/api/graph/subgraph', data: SubgraphDecoder },
  scorecard: { id: 'scorecard', path: '/api/scorecard', data: ScorecardDecoder },
  drift: { id: 'drift', path: '/api/scorecard/drift', data: DriftDecoder },
  disagreement: { id: 'disagreement', path: '/api/scorecard/disagreement', data: DisagreementPageDecoder },
  policyDefaults: { id: 'policyDefaults', path: '/api/policy', data: PolicyDefaultsDecoder },
  allocation: { id: 'allocation', path: '/api/policy/allocate', data: AllocationDecoder },
  dataset: { id: 'dataset', path: '/api/meta/dataset', data: DatasetCardDecoder },
  validation: { id: 'validation', path: '/api/validation', data: ValidationDecoder },
  runtime: { id: 'runtime', path: '/api/meta/run', data: RuntimeMetaDecoder },
} as const satisfies Record<string, RouteSpec<unknown>>;

export const ProblemDecoder = ProblemDetailDecoder;

/**
 * The case routes are keyed on `case_id`, and `case_id` is a 26-character ULID that
 * the API's own path parameter enforces: `Path(min_length=26, max_length=26)` at
 * `apps/api/routers/cases.py:80` and `apps/api/routers/decisions.py:54` and `:138`.
 *
 * An account key (`ACC-7F2A19`, 3-12 chars, `Query(min_length=3, max_length=12)` at
 * `apps/api/routers/graph.py:64`) is a different identifier for a different route. It
 * is what the queue links used to carry here, and it never reached a handler: FastAPI
 * rejects the request while validating the path, so the case workspace and the whole
 * four-eyes flow were unreachable no matter what the response looked like.
 */
export const CASE_ID_LENGTH = 26;

/** `GET /api/cases/{case_id}` — the workspace payload. */
export function casePath(caseId: string): string {
  return `${ROUTES.case.path}/${encodeURIComponent(caseId)}`;
}

/** `POST /api/cases/{case_id}/decisions` — the write that gates the four-eyes flow. */
export function caseDecisionsPath(caseId: string): string {
  return `${casePath(caseId)}/decisions`;
}

/** `POST /api/decisions/{decision_id}/confirm` — the second reviewer (`routers/decisions.py:91`). */
export function decisionConfirmPath(decisionId: string): string {
  return `/api/decisions/${encodeURIComponent(decisionId)}/confirm`;
}

/**
 * SEAM(P7) — routes this app codes against that plan §13/§14 describe but which
 * do not exist in `apps/api/` yet. Each entry is the exact shape the app expects;
 * reconciliation is a one-file diff, not a hunt.
 */
export const SEAMS: { route: string; note: string }[] = [
  { route: 'GET /api/dashboard', note: 'P8a-1 command strip; currency-first figures with r band on each' },
  { route: 'GET /api/alerts', note: 'P8a-2 queue; data.capacity.cutoff_rank drives the capacity line' },
  {
    route: 'GET /api/cases/{case_id} (26-char ULID)',
    note:
      'P8a-3 workspace; the server answers this route with CaseDetail, whose field names are ' +
      "not the client CasePayload decoder's — the read side of the workspace is still unreconciled",
  },
  {
    route: 'POST /api/cases/{case_id}/decisions + POST /api/decisions/{decision_id}/confirm',
    note:
      'P8a-3 writes; body is DecisionCreate, receipt is DecisionWriteResult, and a 409 carries ' +
      'expected_version/current_version/current for the merge view (all three mirrored, not invented)',
  },
  { route: 'GET /api/graph/subgraph', note: 'P8b-4 explorer; edges_by_bucket feeds the time scrubber' },
  {
    route: 'GET /api/scorecard + /drift + /disagreement',
    note: 'P8b-5 studio; scaling constants and the formula string',
  },
  { route: 'GET /api/policy + POST /api/policy/allocate', note: 'P8b-6 simulator; real re-allocation, no theatre' },
  { route: 'GET /api/validation', note: 'P8b-7 every chart served as data, not a screenshot' },
  { route: 'GET /api/meta/run', note: 'deployment_timezone + economics.yaml rendered verbatim' },
  { route: 'GET /api/runs/{run_id}/events', note: 'SSE stage ledger; Last-Event-ID resume' },
];
