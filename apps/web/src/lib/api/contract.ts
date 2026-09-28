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

import {
  type Decoder,
  array,
  boolean,
  integer,
  mapDecode,
  nullable,
  number,
  object,
  oneOf,
  record,
  string,
  unknownDecoder,
} from '../codec';
import {
  type EdgeBucket,
  FLAG_CYCLE,
  FLAG_FLAGGED,
  FLAG_SELF_PAIR,
  type OverlayCounts,
  bucketEdges,
  hopDepths,
  overlayCounts,
} from '../network/derive';
import { ProblemDetailDecoder } from './problem';

/* ============================================================ shared shapes */

/** A JSON scalar — assumption values and rule-parameter values are a string or a
 *  number and nothing else, so it is decoded as that union rather than widened.
 *  `AssumptionLine.value` is `float | int | str` on the model, so a boolean is not a
 *  legal value there and is refused. */
const scalarOrNumber: Decoder<string | number> = {
  kind: 'scalar',
  decode(value, path) {
    if (typeof value === 'string') return { ok: true, value };
    if (typeof value === 'number' && Number.isFinite(value)) return { ok: true, value };
    return { ok: false, error: { path, message: 'expected a string or number' } };
  },
};

/** A scalar from a `dict[str, Any]` bag — `DatasetMeta.sampling` and
 *  `DatasetMeta.deidentification`, both forwarded straight from `config/pipeline.yaml`
 *  (`routers/meta.py`). A YAML `false` is a legal value in those sections and the model
 *  allows anything JSON can hold, so this bag takes booleans too. Deciding with
 *  `scalarOrNumber` here would refuse a config that says `reversible: false`, and a
 *  refused bag is a pane that stops rendering over what is really a keyword in a file. */
const scalarInBag: Decoder<string | number | boolean> = {
  kind: 'bag-scalar',
  decode(value, path) {
    if (typeof value === 'string' || typeof value === 'boolean') return { ok: true, value };
    if (typeof value === 'number' && Number.isFinite(value)) return { ok: true, value };
    return { ok: false, error: { path, message: 'expected a string, number or boolean' } };
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

/**
 * `GET /api/cases/{case_id}` — `Envelope[CaseDetail]` (`apps/api/routers/cases.py:73-81`,
 * the response model built at `:146-176`), `CaseDetail` in
 * `apps/api/schemas/case.py:197-234`.
 *
 * THE SERVED SHAPE IS THE CONTRACT; THE WORKSPACE CONCEPTS ARE DERIVED FROM IT — the
 * pattern section 4 establishes for the explorer, applied here. What follows decodes
 * `CaseDetail` and every nested Pydantic model it references, field for field, and then
 * derives the shape the page renders. The payload this section used to decode —
 * `header{score, run_id, typology, points, reasons, decision_version, scored_at,
 * feature_spec_hash, fusion}`, `contributions[].{label, sentence, source, evidence_ids}`,
 * `evidence[].{amount, typology, txn_ids}`, `transactions[].{channel, rule_codes,
 * balance_delta}`, `rule_hits[].{severity, overlap_group, first_hit}`,
 * `counterfactual.{score_without, expected_value_without}` and a `narrative` block — is a
 * client invention the route has never served, so `/cases/{id}` refused its own 200
 * response and the centre of the demo never rendered. Where a served field has no UI
 * equivalent it is dropped from the derived type, and where the UI wanted a figure the
 * route does not send it is recorded in `apps/web/CONTRACT-GAPS.md`. Nothing is defaulted
 * to zero, an empty string or the current time to make a branch render.
 */

/**
 * The four decision actions the API accepts — `DecisionAction` in
 * `apps/api/schemas/case.py:27`. There is no fifth: a client-side `revisit` reached
 * `DecisionCreate` as an unparseable literal and the whole write died at validation.
 *
 * Declared here, above every decoder that names them, because `object(...)` reads its
 * member decoders at module-initialisation time: `DecisionRecordRow`'s decoder is built
 * while this module is still evaluating, so a const declared further down the file would
 * be in its temporal dead zone and the whole contract module would throw on import.
 */
export const DecisionActionDecoder = oneOf('DecisionAction', 'escalate', 'dismiss', 'review', 'reverse');
export type DecisionAction = 'escalate' | 'dismiss' | 'review' | 'reverse';

/** `four_eyes_state` — `apps/api/schemas/case.py:188` and `:284`. */
export const FourEyesStateDecoder = oneOf('FourEyesState', 'not_required', 'pending', 'confirmed');
export type FourEyesState = 'not_required' | 'pending' | 'confirmed';

/** `ScorecardPointRow` — case.py:36-46, `from_attributes`. `reason_code` stores the
 *  attribute key again rather than a sentence (`landing.py`, `SCORECARD_POINT_SOURCES`:
 *  "the code a case page shows IS the attribute key"), and the human label stays in the
 *  scoring artefact, so neither is a display name. */
type ServedScorecardPointRow = {
  attribute: string;
  bin_label: string;
  points: number;
  woe: number;
  reason_code: string;
  population_share: number;
  bad_rate: number;
};

const ServedScorecardPointRowDecoder: Decoder<ServedScorecardPointRow> = object('ScorecardPointRow', {
  attribute: string,
  bin_label: string,
  points: integer,
  woe: number,
  reason_code: string,
  population_share: number,
  bad_rate: number,
});

/** `CalibrationBand` — case.py:48-115, mirrored against the shape as it stands NOW.
 *
 *  The block is one of two, and `kind` says which (`api.schemas.common.CalibrationKind`,
 *  the two values of `CalibrationResult.confidence_label`, enforced on the row by the
 *  `ck_score_calibration_kind` / pairing constraints added in
 *  `apps/api/alembic/versions/0003_calibration_kind.py`):
 *
 *  * `calibrated_band` — `band`, `observed_rate` and `n` are all present, and the server's
 *    own model validator refuses the block if any of the three is missing;
 *  * `uncalibrated` — no rate, no band, no population, and a `note` in the fold's words
 *    saying why calibration was refused.
 *
 *  So every field except `kind` decodes nullable, which is what the model declares, and
 *  `kind` decodes as required: an answer that does not say which of the two it is has
 *  broken the contract, and refusing it is the point. The client never turns an absent
 *  rate into 0% — plan 03 §H requires the case to *state* the refusal, and DEV-024 names
 *  the confusion this prevents ("0.00 over n=0" reading as a measured zero). */
export const CalibrationKindDecoder = oneOf('CalibrationKind', 'calibrated_band', 'uncalibrated');
export type CalibrationKind = 'calibrated_band' | 'uncalibrated';

type ServedCalibrationBand = {
  kind: CalibrationKind;
  band: string | null;
  observed_rate: number | null;
  n: number | null;
  note: string | null;
};

const ServedCalibrationBandDecoder: Decoder<ServedCalibrationBand> = object('CalibrationBand', {
  kind: CalibrationKindDecoder,
  band: nullable(string),
  observed_rate: nullable(number),
  n: nullable(integer),
  note: nullable(string),
});

/** `MonteCarlo` — case.py:91-99. Percentiles, not a lower/upper pair: `p50` is served and
 *  the client that invented `max_depth` never received one. */
type ServedMonteCarlo = {
  runs: number;
  seed: number;
  p05: Money;
  p50: Money;
  p95: Money;
  interval: number[];
};

const ServedMonteCarloDecoder: Decoder<ServedMonteCarlo> = object('MonteCarlo', {
  runs: integer,
  seed: integer,
  p05: MoneyDecoder,
  p50: MoneyDecoder,
  p95: MoneyDecoder,
  interval: array(number),
});

/** `EconomicsBlock` — case.py:59-88. Every money field is a bare `Money`; the recovery
 *  band arrives as rates (`list[float]`), never as money at each rate, so no band leg can
 *  be rendered without the server pricing it first. */
type ServedEconomicsBlock = {
  exposure: Money;
  expected_value: Money;
  loss_avoided: Money;
  analyst_minutes: number;
  analyst_cost: Money;
  friction_cost: Money;
  recovery_rate: number;
  recovery_sensitivity_band: number[];
  assumptions: AssumptionLine[];
  monte_carlo: ServedMonteCarlo | null;
};

const ServedEconomicsBlockDecoder: Decoder<ServedEconomicsBlock> = object('EconomicsBlock', {
  exposure: MoneyDecoder,
  expected_value: MoneyDecoder,
  loss_avoided: MoneyDecoder,
  analyst_minutes: number,
  analyst_cost: MoneyDecoder,
  friction_cost: MoneyDecoder,
  recovery_rate: number,
  recovery_sensitivity_band: array(number),
  assumptions: array(AssumptionLineDecoder),
  monte_carlo: nullable(ServedMonteCarloDecoder),
});

/** `EvidenceRow` — case.py:102-112. `kind` and `label` are stored strings, not a closed
 *  set, and `detail` is a `dict[str, Any]` bag: the event's own payload, whose keys the
 *  producer chooses per kind. */
type ServedEvidenceRow = {
  id: number;
  occurred_at: string;
  kind: string;
  label: string;
  txn_id: string | null;
  rule_id: string | null;
  object_key: string | null;
  detail: Record<string, unknown>;
};

const ServedEvidenceRowDecoder: Decoder<ServedEvidenceRow> = object('EvidenceRow', {
  id: integer,
  occurred_at: TimestampDecoder,
  kind: string,
  label: string,
  txn_id: nullable(string),
  rule_id: nullable(string),
  object_key: nullable(string),
  detail: record(unknownDecoder),
});

/** `TransactionRow` — case.py:115-131. Balances are raw minor-unit integers with no
 *  currency attached (`routers/cases.py:_transaction_row` forwards the stored column
 *  untouched), so this client does not dress them in one. `event_date_local` is a stored
 *  `date`, which JSON serialises as a calendar string. */
type ServedTransactionRow = {
  txn_id: string;
  event_ts_utc: string;
  local_hour: number;
  event_date_local: string | null;
  src_account_key: string | null;
  dst_account_key: string | null;
  amount: Money;
  txn_type: string;
  src_balance_before: number | null;
  src_balance_after: number | null;
  dst_balance_before: number | null;
  dst_balance_after: number | null;
  label_fraud: boolean | null;
  label_typology: string | null;
};

const ServedTransactionRowDecoder: Decoder<ServedTransactionRow> = object('TransactionRow', {
  txn_id: string,
  event_ts_utc: TimestampDecoder,
  local_hour: integer,
  event_date_local: nullable(string),
  src_account_key: nullable(string),
  dst_account_key: nullable(string),
  amount: MoneyDecoder,
  txn_type: string,
  src_balance_before: nullable(integer),
  src_balance_after: nullable(integer),
  dst_balance_before: nullable(integer),
  dst_balance_after: nullable(integer),
  label_fraud: nullable(boolean),
  label_typology: nullable(string),
});

/** `RuleHitRow` — case.py:134-143. `observed` and `threshold` are numbers, not a
 *  sentence, and `detail` is again a producer-chosen bag. */
type ServedRuleHitRow = {
  rule_id: string;
  rule_name: string;
  typology: string;
  fired: boolean;
  observed: number | null;
  threshold: number | null;
  detail: Record<string, unknown>;
};

const ServedRuleHitRowDecoder: Decoder<ServedRuleHitRow> = object('RuleHitRow', {
  rule_id: string,
  rule_name: string,
  typology: string,
  fired: boolean,
  observed: nullable(number),
  threshold: nullable(number),
  detail: record(unknownDecoder),
});

/** `ShapRow` — case.py:146-153. The contribution the waterfall draws, with the ids of the
 *  transactions that carry its evidence. There is no label and no sentence here: the
 *  feature key IS the label the response offers. */
type ServedShapRow = {
  feature: string;
  shap: number;
  feature_value: number | null;
  rank: number;
  evidence_txn_ids: string[];
};

const ServedShapRowDecoder: Decoder<ServedShapRow> = object('ShapRow', {
  feature: string,
  shap: number,
  feature_value: nullable(number),
  rank: integer,
  evidence_txn_ids: array(string),
});

/** `WatchlistEnrichment` — case.py:156-170. Advisory screening, structurally incapable of
 *  being a decision, and it says so in two places. */
type ServedWatchlistEnrichment = {
  list_name: string;
  list_version: string;
  record_count: number;
  hits: Record<string, unknown>[];
  advisory_only: boolean;
  note: string;
};

const ServedWatchlistEnrichmentDecoder: Decoder<ServedWatchlistEnrichment> = object('WatchlistEnrichment', {
  list_name: string,
  list_version: string,
  record_count: integer,
  hits: array(record(unknownDecoder)),
  advisory_only: boolean,
  note: string,
});

/** `DecisionRecordRow` — case.py:173-194. `prev_hash` is non-null on the server: the
 *  genesis row carries a declared empty-chain hash rather than a missing one. */
type ServedDecisionRecordRow = {
  decision_id: string;
  decision_seq: number;
  chain_seq: number;
  action: DecisionAction;
  reason: string;
  actor_id: string;
  actor_roles: string[];
  occurred_at: string;
  exposure: Money;
  four_eyes_required: boolean;
  four_eyes_state: FourEyesState;
  confirmed_by: string | null;
  confirmed_at: string | null;
  reversal_of_decision_id: string | null;
  decided_on_superseded_run: boolean;
  prev_hash: string;
  row_hash: string;
};

const ServedDecisionRecordRowDecoder: Decoder<ServedDecisionRecordRow> = object('DecisionRecordRow', {
  decision_id: string,
  decision_seq: integer,
  chain_seq: integer,
  action: DecisionActionDecoder,
  reason: string,
  actor_id: string,
  actor_roles: array(string),
  occurred_at: TimestampDecoder,
  exposure: MoneyDecoder,
  four_eyes_required: boolean,
  four_eyes_state: FourEyesStateDecoder,
  confirmed_by: nullable(string),
  confirmed_at: nullable(TimestampDecoder),
  reversal_of_decision_id: nullable(string),
  decided_on_superseded_run: boolean,
  prev_hash: string,
  row_hash: string,
});

/** The `reason_codes` bag. `CaseDetail.reason_codes` is `list[dict[str, Any]]`, and
 *  `routers/alerts.py:_reasons` documents that the stored column exists in two shapes
 *  across pipeline versions — a list of bare sentences and a list of `{code,label,points}`
 *  maps — so the item is decoded as the union the producer actually issues. `points` is
 *  null when the record carries none, never 0: zero points is a claim about the scorecard
 *  and null is a claim about the record. */

/** The WIRE form of one item — what a route sends and what a double must write. */
export type ServedReasonCodeMapWire = {
  code?: string;
  reason_code?: string;
  label?: string;
  description?: string;
  points?: number | null;
};
export type ServedReasonCodeWire = string | ServedReasonCodeMapWire;

/** The normalised form the decoder produces, so `deriveCasePayload` never branches on
 *  which pipeline version wrote the row. */
type ServedReasonCode =
  | { readonly shape: 'map'; readonly code: string; readonly label: string; readonly points: number | null }
  | { readonly shape: 'sentence'; readonly text: string };

const ServedReasonCodeDecoder: Decoder<ServedReasonCode> = {
  kind: 'reason_code',
  decode(value, path) {
    if (typeof value === 'string') return { ok: true, value: { shape: 'sentence', text: value } };
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      return { ok: false, error: { path, message: 'expected a reason sentence or a reason map' } };
    }
    const row = value as Record<string, unknown>;
    const rawCode = row.code ?? row.reason_code;
    const rawLabel = row.label ?? row.description;
    const rawPoints = row.points;
    if (typeof rawPoints === 'number' && !Number.isFinite(rawPoints)) {
      return { ok: false, error: { path: `${path}.points`, message: 'expected a finite number' } };
    }
    return {
      ok: true,
      value: {
        shape: 'map',
        code: typeof rawCode === 'string' ? rawCode : 'unspecified',
        label: typeof rawLabel === 'string' ? rawLabel : '',
        points: typeof rawPoints === 'number' ? rawPoints : null,
      },
    };
  },
};

/** One candidate change from `routers/cases.py:counterfactual` (:507-599) — arithmetic
 *  over stored integer points and stored band boundaries, never a second model call. */
const ServedCounterfactualChangeDecoder = object('cheapest_change', {
  attribute: string,
  from_bin: string,
  to_bin: string,
  points_delta: integer,
  resulting_points: integer,
  resulting_band: BandDecoder,
  resulting_action: string,
});
export type ServedCounterfactualChange = typeof ServedCounterfactualChangeDecoder extends Decoder<infer T> ? T : never;

/** The WIRE form of the `counterfactual` bag. The route sends seven keys when a band-crossing
 *  change exists and four when none does (`possible`, `current_band`, `total_points`, `note`
 *  — `routers/cases.py:578-586`), so everything else is genuinely absent on the wire rather
 *  than null. A double must be able to write the shape the route writes, which is why the
 *  optional-ness lives here and not in the decoded type. */
export type ServedCounterfactualWire = {
  possible: boolean;
  current_band: Band;
  total_points: number;
  note?: string | null;
  current_action?: string | null;
  basis?: string | null;
  cheapest_change?: ServedCounterfactualChange | null;
  alternatives?: ServedCounterfactualChange[];
};

const ServedCounterfactualDecoder = object('counterfactual', {
  possible: boolean,
  current_band: BandDecoder,
  current_action: nullable(string),
  total_points: integer,
  note: nullable(string),
  basis: nullable(string),
  cheapest_change: nullable(ServedCounterfactualChangeDecoder),
  // Nullable because the impossible-shape response omits the key outright; the derivation
  // reads an omitted list as "no alternatives were served", never as an empty claim.
  alternatives: nullable(array(ServedCounterfactualChangeDecoder)),
});
type ServedCounterfactual = typeof ServedCounterfactualDecoder extends Decoder<infer T> ? T : never;

/** `CaseStatus` — case.py:228 with the literal at `catalog.py:28`. */
export const CaseStatusDecoder = oneOf('CaseStatus', 'open', 'pending_four_eyes', 'decided', 'reversed');
export type CaseStatus = 'open' | 'pending_four_eyes' | 'decided' | 'reversed';

/** `CaseDetail` — case.py:197-234, all twenty-eight declared fields. */
export type ServedCaseDetail = {
  case_id: string;
  pinned_run_id: string;
  run_state: string;
  superseded: boolean;
  account_key: string;
  band: Band;
  fused_score: number;
  scorecard_points_total: number;
  scorecard_points: ServedScorecardPointRow[];
  calibration: ServedCalibrationBand;
  predicted_typology: string | null;
  model_version: string;
  reason_codes: ServedReasonCode[];
  rule_ids: string[];
  economics: ServedEconomicsBlock;
  evidence: ServedEvidenceRow[];
  transactions: ServedTransactionRow[];
  transaction_total: number;
  shap: ServedShapRow[];
  rule_hits: ServedRuleHitRow[];
  watchlist: ServedWatchlistEnrichment | null;
  decision_history: ServedDecisionRecordRow[];
  case_version: number;
  status: CaseStatus;
  rank_under_active_policy: number | null;
  counterfactual: ServedCounterfactual | null;
};

/** `CaseDetail` as the WIRE sends it, for the fixture double in `src/fixtures/`.
 *
 *  Two fields differ from the decoded shape, and only because the decoder normalises them:
 *  `reason_codes` arrives as bare sentences or `{code,label,points}` maps depending on the
 *  pipeline version that wrote the row, and `counterfactual` arrives with different key sets
 *  depending on whether a band-crossing change exists. A double typed against the DECODED
 *  shape would have to pre-normalise its own bytes, which is precisely the agreement-with-
 *  the-client this file exists to forbid. Everything else is identical. */
export type ServedCaseDetailWire = Omit<ServedCaseDetail, 'reason_codes' | 'counterfactual'> & {
  reason_codes: ServedReasonCodeWire[];
  counterfactual: ServedCounterfactualWire | null;
};

export const ServedCaseDetailDecoder: Decoder<ServedCaseDetail> = object('CaseDetail', {
  case_id: string,
  pinned_run_id: string,
  run_state: string,
  superseded: boolean,
  account_key: string,
  band: BandDecoder,
  fused_score: number,
  scorecard_points_total: integer,
  scorecard_points: array(ServedScorecardPointRowDecoder),
  calibration: ServedCalibrationBandDecoder,
  predicted_typology: nullable(string),
  model_version: string,
  reason_codes: array(ServedReasonCodeDecoder),
  rule_ids: array(string),
  economics: ServedEconomicsBlockDecoder,
  evidence: array(ServedEvidenceRowDecoder),
  transactions: array(ServedTransactionRowDecoder),
  transaction_total: integer,
  shap: array(ServedShapRowDecoder),
  rule_hits: array(ServedRuleHitRowDecoder),
  watchlist: nullable(ServedWatchlistEnrichmentDecoder),
  decision_history: array(ServedDecisionRecordRowDecoder),
  case_version: integer,
  status: CaseStatusDecoder,
  rank_under_active_policy: nullable(integer),
  counterfactual: nullable(ServedCounterfactualDecoder),
});

/* ------------------------------------------- the workspace's own shapes --- */

/** One scorecard point as the rail lists it. The invented `label` and `is_reason_code` are
 *  gone: the row's own key is the only name the response offers for the attribute, and
 *  whether an attribute was quoted as a reason is not a column the run stores per point. */
export type ScorecardPoints = {
  attribute: string;
  bin: string;
  points: number;
  woe: number;
  population_share: number;
  bad_rate: number;
};

/** The stored Monte Carlo draw. `lower`/`upper` are `p05`/`p95` under the names the
 *  interval strip uses, and `centre` is the served `p50` — a value the previous shape
 *  dropped rather than one it lacked. `max_depth` is not on the wire (CONTRACT-GAPS). */
export type MonteCarloInterval = {
  lower: Money;
  centre: Money;
  upper: Money;
  runs: number;
  seed: number;
  interval: number[];
};

/** `MoneyFigure` from a bare served `Money`. `band` stays null because the response sends
 *  the recovery band as rates, not as money priced at each rate; putting a money figure
 *  behind those rates would be arithmetic this client has no licence to run. */
function moneyAsFigure(value: Money, rates: number[]): MoneyFigure {
  return { value, band: null, band_rates: rates };
}

export type CaseEconomics = {
  exposure: MoneyFigure;
  expected_value: MoneyFigure;
  loss_avoided: MoneyFigure;
  review_minutes: number;
  analyst_cost: MoneyFigure;
  friction_cost: Money;
  monte_carlo: MonteCarloInterval | null;
  recovery_rate: number;
  recovery_sensitivity_band: number[];
  assumptions: AssumptionLine[];
};

/** Confidence for the case header, as a labelled state rather than as a number that may
 *  not have been measured. `state` is the served `kind`, verbatim: the response says which
 *  of the two readings this is, so the client never has to infer it from a null. */
export type CaseCalibration = {
  state: CalibrationKind;
  /** Null for an uncalibrated block — there is no band the rate was measured over. */
  band: string | null;
  observed_rate: number | null;
  n: number | null;
  /** The run's own words for why calibration was refused; null when it was not refused. */
  reason: string | null;
};

export type CaseHeader = {
  case_id: string;
  account_key: string;
  /** `pinned_run_id`: the run this case's evidence was read from, not the newest one. */
  run_id: string;
  run_state: string;
  superseded: boolean;
  score: number;
  scorecard_points_total: number;
  band: Band;
  calibration: CaseCalibration;
  /** Served as a free string. `typologyMetaOf` reads it, and the header prints the served
   *  id whether or not it is one of the twelve this product has glyphs for. */
  typology: string | null;
  model_version: string;
  rule_ids: string[];
  status: CaseStatus;
  rank_under_active_policy: number | null;
  points: ScorecardPoints[];
  reasons: string[];
  economics: CaseEconomics;
  watchlist: WatchlistEnrichment | null;
};

/** `WatchlistEnrichment`, carried through so the rail can state that screening happened
 *  and that it decided nothing. `hits` stays the served bag: its keys belong to the
 *  screening source, and this client does not guess at them. */
export type WatchlistEnrichment = {
  list_name: string;
  list_version: string;
  record_count: number;
  hits: Record<string, unknown>[];
  advisory_only: boolean;
  note: string;
};

export type EvidenceEvent = {
  id: string;
  ts_utc: string;
  /** The stored kind string, not a five-member set the response never declares. */
  kind: string;
  title: string;
  /** The served `detail` bag rendered as `key value` pairs. Formatting, not composition:
   *  every token in it is a served scalar, and an empty bag says it is empty rather than
   *  rendering as a blank cell. */
  detail: string;
  txn_id: string | null;
  rule_id: string | null;
  object_key: string | null;
};

export type TransactionRow = {
  txn_id: string;
  ts_utc: string;
  local_hour: number;
  event_date_local: string | null;
  type: string;
  amount: Money;
  src_account_key: string | null;
  dst_account_key: string | null;
  /** The endpoint that is not this case's account, when exactly one of the two is. */
  counterparty_key: string | null;
  /** `in` when this account is the destination, `out` when it is the source. Null states
   *  that neither endpoint is the case's account — which the route's own merge of the two
   *  side-filtered queries makes reachable, and a fabricated direction would hide. */
  direction: 'in' | 'out' | null;
  is_zero_value: boolean;
  label_fraud: boolean | null;
  label_typology: string | null;
};

/** One persisted SHAP contribution. `label`, `sentence`, `evidence_ids` and `source` were
 *  client inventions; `direction` is the sign of the served value, which is what the
 *  waterfall's increasing/decreasing split means. */
export type Contribution = {
  feature: string;
  value: number;
  feature_value: number | null;
  direction: 'increases' | 'decreases';
  rank: number;
  /** The cross-filter key: clicking this row filters the other panes to these ids. */
  txn_ids: string[];
};

export type RuleHit = {
  rule_code: string;
  rule_name: string;
  typology: string;
  fired: boolean;
  observed: number | null;
  threshold: number | null;
  /** The served `detail` bag, scalar entries only, in key order. */
  parameters: { key: string; value: string | number | boolean }[];
};

/** The cheapest single stored-bin change that crosses a band line, as the route computes
 *  it. Note what it is measured in: integer scorecard POINTS, not a probability. The
 *  shape this client used to decode — `score_without` printed to three decimals beside a
 *  band badge, and an `expected_value_without` money figure — asked a points total to read
 * * as a score and asked for a re-priced EV the route never recomputed. */
export type CounterfactualChange = {
  attribute: string;
  from_bin: string;
  to_bin: string;
  points_delta: number;
  resulting_points: number;
  resulting_band: Band;
  resulting_action: string;
};

export type Counterfactual = {
  possible: boolean;
  current_band: Band;
  current_action: string | null;
  total_points: number;
  change: CounterfactualChange | null;
  alternatives: CounterfactualChange[];
  /** The route's own words: the `note` when no single change moves the band, the `basis`
   *  when one does. Null only when the response carried neither. */
  statement: string | null;
};

/** One append-only decision, with its chain position. */
export type Decision = {
  seq: number;
  decision: DecisionAction;
  reason: string;
  actor: string;
  /** `actor_roles` — a reviewer can hold several, so it stays a list rather than being
   *  collapsed into one word by a client that has to pick. */
  roles: string[];
  recorded_at: string;
  hash: string;
  prev_hash: string | null;
  /** `reversal_of_decision_id`: a decision id, not a sequence number. */
  reversible_of: string | null;
  four_eyes_required: boolean;
  four_eyes_state: FourEyesState;
  decided_on_superseded_run: boolean;
  /** Local-only: true while the write is in flight and unconfirmed by the server. */
  pending?: boolean | undefined;
};

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
  /** `transaction_total` is the count matching the filter before the route's own
   *  `TRANSACTION_CAP` (`routers/cases.py:59`) truncated the list, so the table can say how
   *  many rows it is not showing rather than implying the window was quiet. */
  transaction_total: number;
};

/* ------------------------------------------------------------- derivation -- */

/** The served `detail` bag rendered for one line: scalar entries as `key value`, in the
 *  key order the JSON arrived in. Nested values are skipped rather than stringified as
 *  `[object Object]`, and an empty bag is named as empty. */
function renderBag(bag: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of Object.keys(bag)) {
    const value = bag[key];
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      parts.push(`${key} ${String(value)}`);
    }
  }
  return parts.length > 0 ? parts.join(' · ') : 'no detail stored on this event';
}

/** The served bag's scalar entries as pairs, for the rule-hit parameter list. */
function scalarEntries(bag: Record<string, unknown>): { key: string; value: string | number | boolean }[] {
  return Object.keys(bag)
    .filter((key) => {
      const value = bag[key];
      return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
    })
    .map((key) => ({ key, value: bag[key] as string | number | boolean }));
}

/** `CalibrationBand` → the header's confidence block. Every value passes through under its
 *  served name; the only thing added is that `state` is the served `kind`. Nothing is
 *  defaulted: an uncalibrated block arrives with a null rate, a null band and a null
 *  population, and it is rendered as a refusal, not as a measurement of zero. */
function deriveCalibration(served: ServedCalibrationBand): CaseCalibration {
  return {
    state: served.kind,
    band: served.band,
    observed_rate: served.observed_rate,
    n: served.n,
    reason: served.note,
  };
}

/** Which side of the transfer is the counterparty. The route assembled this row from the
 *  `src_account_key` / `dst_account_key` queries it ran for this exact account
 *  (`routers/cases.py:_transactions`), so naming the other endpoint is the same read it
 *  already made — not a guess from a column the client has to interpret. */
function deriveCounterparty(served: ServedTransactionRow, accountKey: string): string | null {
  const isSource = served.src_account_key === accountKey;
  const isDestination = served.dst_account_key === accountKey;
  if (isSource && !isDestination) return served.dst_account_key;
  if (isDestination && !isSource) return served.src_account_key;
  // Both sides this account (a self-transfer) or neither: no counterparty to name, and no
  // direction to claim.
  return isSource && isDestination ? null : (served.dst_account_key ?? served.src_account_key ?? null);
}

function deriveDirection(served: ServedTransactionRow, accountKey: string): 'in' | 'out' | null {
  const isSource = served.src_account_key === accountKey;
  const isDestination = served.dst_account_key === accountKey;
  if (isDestination && !isSource) return 'in';
  if (isSource && !isDestination) return 'out';
  return null;
}

/** The whole derivation, in one pure place so the unit test can drive it directly. */
export function deriveCasePayload(served: ServedCaseDetail): CasePayload {
  const economics = served.economics;
  return {
    header: {
      case_id: served.case_id,
      account_key: served.account_key,
      run_id: served.pinned_run_id,
      run_state: served.run_state,
      superseded: served.superseded,
      score: served.fused_score,
      scorecard_points_total: served.scorecard_points_total,
      band: served.band,
      calibration: deriveCalibration(served.calibration),
      typology: served.predicted_typology,
      model_version: served.model_version,
      rule_ids: served.rule_ids,
      status: served.status,
      rank_under_active_policy: served.rank_under_active_policy,
      points: served.scorecard_points.map(
        (row): ScorecardPoints => ({
          attribute: row.attribute,
          bin: row.bin_label,
          points: row.points,
          woe: row.woe,
          population_share: row.population_share,
          bad_rate: row.bad_rate,
        }),
      ),
      // The served reason bag, in the served order. A `{code,label,points}` map prints its
      // label and, when the record carries one, its signed points; a bare sentence prints
      // as the sentence it is. Nothing is looked up, inferred or padded.
      reasons: served.reason_codes.map((code) =>
        code.shape === 'sentence'
          ? code.text
          : code.points === null
            ? `${code.label.length > 0 ? code.label : code.code}`
            : `${code.label.length > 0 ? code.label : code.code} (${String(code.points)} points)`,
      ),
      economics: {
        exposure: moneyAsFigure(economics.exposure, economics.recovery_sensitivity_band),
        expected_value: moneyAsFigure(economics.expected_value, economics.recovery_sensitivity_band),
        loss_avoided: moneyAsFigure(economics.loss_avoided, economics.recovery_sensitivity_band),
        review_minutes: economics.analyst_minutes,
        analyst_cost: moneyAsFigure(economics.analyst_cost, economics.recovery_sensitivity_band),
        friction_cost: economics.friction_cost,
        recovery_rate: economics.recovery_rate,
        recovery_sensitivity_band: economics.recovery_sensitivity_band,
        assumptions: economics.assumptions,
        monte_carlo:
          economics.monte_carlo === null
            ? null
            : {
                lower: economics.monte_carlo.p05,
                centre: economics.monte_carlo.p50,
                upper: economics.monte_carlo.p95,
                runs: economics.monte_carlo.runs,
                seed: economics.monte_carlo.seed,
                interval: economics.monte_carlo.interval,
              },
      },
      watchlist:
        served.watchlist === null
          ? null
          : {
              list_name: served.watchlist.list_name,
              list_version: served.watchlist.list_version,
              record_count: served.watchlist.record_count,
              hits: served.watchlist.hits,
              advisory_only: served.watchlist.advisory_only,
              note: served.watchlist.note,
            },
    },
    evidence: served.evidence.map(
      (row): EvidenceEvent => ({
        // The served id is an integer; the DOM needs a key, and `String` of a served value
        // is that key rather than a composed one.
        id: String(row.id),
        ts_utc: row.occurred_at,
        kind: row.kind,
        title: row.label,
        detail: renderBag(row.detail),
        txn_id: row.txn_id,
        rule_id: row.rule_id,
        object_key: row.object_key,
      }),
    ),
    transactions: served.transactions.map(
      (row): TransactionRow => ({
        txn_id: row.txn_id,
        ts_utc: row.event_ts_utc,
        local_hour: row.local_hour,
        event_date_local: row.event_date_local,
        type: row.txn_type,
        amount: row.amount,
        src_account_key: row.src_account_key,
        dst_account_key: row.dst_account_key,
        counterparty_key: deriveCounterparty(row, served.account_key),
        direction: deriveDirection(row, served.account_key),
        is_zero_value: row.amount.minor === 0,
        label_fraud: row.label_fraud,
        label_typology: row.label_typology,
      }),
    ),
    contributions: served.shap.map(
      (row): Contribution => ({
        feature: row.feature,
        value: row.shap,
        feature_value: row.feature_value,
        direction: row.shap < 0 ? 'decreases' : 'increases',
        rank: row.rank,
        txn_ids: row.evidence_txn_ids,
      }),
    ),
    rule_hits: served.rule_hits.map(
      (row): RuleHit => ({
        rule_code: row.rule_id,
        rule_name: row.rule_name,
        typology: row.typology,
        fired: row.fired,
        observed: row.observed,
        threshold: row.threshold,
        parameters: scalarEntries(row.detail),
      }),
    ),
    counterfactual:
      served.counterfactual === null
        ? null
        : {
            possible: served.counterfactual.possible,
            current_band: served.counterfactual.current_band,
            current_action: served.counterfactual.current_action,
            total_points: served.counterfactual.total_points,
            change: served.counterfactual.cheapest_change === null ? null : served.counterfactual.cheapest_change,
            alternatives: served.counterfactual.alternatives ?? [],
            statement: served.counterfactual.note ?? served.counterfactual.basis ?? null,
          },
    decisions: served.decision_history.map(
      (row): Decision => ({
        seq: row.decision_seq,
        decision: row.action,
        reason: row.reason,
        actor: row.actor_id,
        roles: row.actor_roles,
        recorded_at: row.occurred_at,
        hash: row.row_hash,
        prev_hash: row.prev_hash,
        reversible_of: row.reversal_of_decision_id,
        four_eyes_required: row.four_eyes_required,
        four_eyes_state: row.four_eyes_state,
        decided_on_superseded_run: row.decided_on_superseded_run,
      }),
    ),
    decision_version: served.case_version,
    transaction_total: served.transaction_total,
  };
}

export const CasePayloadDecoder: Decoder<CasePayload> = mapDecode(
  ServedCaseDetailDecoder,
  deriveCasePayload,
  'CasePayload',
);

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

/**
 * `GET /api/graph/subgraph` — `NetworkSubgraph` in `apps/api/schemas/catalog.py:283-302`,
 * built by `apps/api/routers/graph.py:220-302`.
 *
 * THE SERVED SHAPE IS THE CONTRACT; THE UI CONCEPTS ARE DERIVED FROM IT. The decoder
 * below accepts exactly the field names the response model declares and nothing else,
 * and the three things the explorer shows that the route does not send — per-node hop
 * distance, the scrubber's edge-formation buckets, the overlay counts — are computed in
 * `lib/network/derive.ts` from those served bytes. Each derivation is the same
 * computation the server already runs on the same edge set, and where the server has a
 * concept at all: an absent flag counts zero and the chip that would use it renders
 * disabled and says why, rather than offering a toggle that selects nothing.
 *
 * Nothing here carries a field the model does not declare. The types this section used
 * to decode — `key`, `is_cycle_member` as a served boolean, `typology`, `is_reversal`,
 * `high_velocity`, `edges_by_bucket`, `window{from,to}`, `cap`, and an
 * `overlays{...}` object on the wire — are client inventions, and the invented fields
 * are gone from `GraphEdge` rather than kept as branches that can never fire.
 */

/** `NetworkNode` — catalog.py:238-249. `id` is an account key, never a raw identifier
 *  (03 §D), and `flags` is a `list[str]` of the flag names `_node_flags` wrote. */
export type ServedNetworkNode = {
  id: string;
  label: string;
  band: Band | null;
  exposure: Money | null;
  degree: number;
  community_id: number | null;
  is_seed: boolean;
  is_rail: boolean;
  flags: string[];
};

const ServedNetworkNodeDecoder: Decoder<ServedNetworkNode> = object('NetworkNode', {
  id: string,
  label: string,
  band: nullable(BandDecoder),
  exposure: nullable(MoneyDecoder),
  degree: integer,
  community_id: nullable(integer),
  is_seed: boolean,
  is_rail: boolean,
  flags: array(string),
});

/** `NetworkEdge` — catalog.py:252-261. There is no edge id on the wire: `(run_id, src,
 *  dst)` is unique per `uq_graph_edge`, so the explorer's element key is composed from
 *  the two served endpoints. `txn_count`, not `count`; `first_ts`/`last_ts`, not
 *  `ts_first`/`ts_last`; `flags` is `["self_pair"]` or nothing today. */
export type ServedNetworkEdge = {
  source: string;
  target: string;
  total: Money;
  txn_count: number;
  first_ts: string;
  last_ts: string;
  flags: string[];
};

const ServedNetworkEdgeDecoder: Decoder<ServedNetworkEdge> = object('NetworkEdge', {
  source: string,
  target: string,
  total: MoneyDecoder,
  txn_count: integer,
  first_ts: TimestampDecoder,
  last_ts: TimestampDecoder,
  flags: array(string),
});

/** `CommunityMetaNode` — catalog.py:264-283. `member_count` is the community's stored
 *  true size, which is what a meta-node's label has to carry; `total` is nullable
 *  because an unpriced community is served as null rather than as zero. */
export type ServedCommunityMetaNode = {
  community_id: number;
  member_count: number;
  total: Money | null;
  representative_account_key: string;
};

const ServedCommunityMetaNodeDecoder: Decoder<ServedCommunityMetaNode> = object('CommunityMetaNode', {
  community_id: integer,
  member_count: integer,
  total: nullable(MoneyDecoder),
  representative_account_key: string,
});

export type ServedNetworkSubgraph = {
  run_id: string;
  seed_account_key: string;
  hops: number;
  nodes: ServedNetworkNode[];
  edges: ServedNetworkEdge[];
  collapsed_communities: ServedCommunityMetaNode[];
  node_cap: number;
  truncated: boolean;
  truncation_reason: string | null;
  window_start: string | null;
  window_end: string | null;
  counterparty_note: string | null;
};

const ServedNetworkSubgraphDecoder: Decoder<ServedNetworkSubgraph> = object('NetworkSubgraph', {
  run_id: string,
  seed_account_key: string,
  hops: integer,
  nodes: array(ServedNetworkNodeDecoder),
  edges: array(ServedNetworkEdgeDecoder),
  collapsed_communities: array(ServedCommunityMetaNodeDecoder),
  node_cap: integer,
  truncated: boolean,
  truncation_reason: nullable(string),
  window_start: nullable(TimestampDecoder),
  window_end: nullable(TimestampDecoder),
  counterparty_note: nullable(string),
});

/** The explorer's node, after derivation. `key`/`node_type`/`flagged`/`is_cycle_member`/
 *  `hops`/`true_size` are the names the canvas and the rail read; every one of them is
 *  computed above this line from a served field, and each is commented at its derivation.
 *  There is no `'external'` member: the response model has no notion of an external
 *  party, so the canvas has no rule for one either. */
export type GraphNode = {
  key: string;
  label: string;
  band: Band | null;
  exposure: Money | null;
  degree: number;
  community_id: number | null;
  is_seed: boolean;
  is_rail: boolean;
  /** Served, verbatim. */
  flags: string[];
  node_type: 'account' | 'rail' | 'meta';
  flagged: boolean;
  is_cycle_member: boolean;
  /** Derived by BFS from `seed_account_key`; null when the walk found no path inside
   *  the route's own four-hop ceiling. The UI has to say that, not print a sentinel. */
  hops: number | null;
  /** `member_count` of the collapsed community whose representative this node is. */
  true_size: number | null;
};

/** The explorer's edge: the served fields, renamed only where the wire name is awkward
 *  in TS (`txn_count` → `count`), plus `id` composed from the served endpoints. */
export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  ts_first: string;
  ts_last: string;
  count: number;
  total: Money;
  flags: string[];
  /** A self-transfer, `A -> A`. The one edge flag the pipeline writes. */
  self_pair: boolean;
};

export type Subgraph = {
  run_id: string;
  seed_account_key: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  truncation_reason: string | null;
  /** `node_cap`, the server-side ceiling the response names. */
  cap: number;
  collapsed_communities: ServedCommunityMetaNode[];
  /** Both bounds are nullable on the wire (`window_start`/`window_end`), and an absent
   *  bound is an unbounded window: the UI says so rather than printing an epoch. */
  window: { from: string | null; to: string | null };
  /** The route's own sentence for an account with no counterparties in the window
   *  (`counterparty_note`, catalog.py:296-301) — quoted by the empty state rather than
   *  paraphrased, because the window and the radius that produced it are server facts. */
  counterparty_note: string | null;
  /** How many edges the bucket list leaves out because they carry no parseable
   *  `first_ts`. Zero for a payload the server can build, but it is stated rather than
   *  assumed away, because the scrubber's replay silently omits them. */
  unbucketed_edges: number;
  /** Client-derived from the served `first_ts` values. Drives the time scrubber. */
  edges_by_bucket: EdgeBucket[];
  hops: number;
  /** Client-derived counts of the nodes that carry each flag. */
  overlays: OverlayCounts;
};

/** The whole derivation, in one pure place so the unit test can drive it directly. */
function deriveSubgraph(served: ServedNetworkSubgraph): Subgraph {
  const depths = hopDepths(served.seed_account_key, served.edges);
  /* A meta-node is identified the way the response identifies it: its key is a
     collapsed community's `representative_account_key`. `is_rail` wins over it because
     the rail flag is stored per edge endpoint and is the stronger statement about the
     node, and the graph layer's collapse policy never collapses the seed's own
     community, so the two never compete in practice. */
  const sizeByRepresentative = new Map(
    served.collapsed_communities.map((entry) => [entry.representative_account_key, entry.member_count]),
  );
  const nodes = served.nodes.map((node): GraphNode => {
    const trueSize = sizeByRepresentative.get(node.id) ?? null;
    return {
      key: node.id,
      label: node.label,
      band: node.band,
      exposure: node.exposure,
      degree: node.degree,
      community_id: node.community_id,
      is_seed: node.is_seed,
      is_rail: node.is_rail,
      flags: node.flags,
      node_type: node.is_rail ? 'rail' : trueSize !== null ? 'meta' : 'account',
      // `_node_flags` adds "flagged" for a D or E band, and "cycle" only if an edge
      // touching this account carried it — which the pipeline does not write today.
      flagged: node.flags.includes(FLAG_FLAGGED),
      is_cycle_member: node.flags.includes(FLAG_CYCLE),
      hops: depths.get(node.id) ?? null,
      true_size: trueSize,
    };
  });
  const edges = served.edges.map(
    (edge): GraphEdge => ({
      // Composed from the two served endpoints; `(run_id, src, dst)` is unique per
      // uq_graph_edge, so this is an identity and not a fabricated field.
      id: `${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      ts_first: edge.first_ts,
      ts_last: edge.last_ts,
      count: edge.txn_count,
      total: edge.total,
      flags: edge.flags,
      self_pair: edge.flags.includes(FLAG_SELF_PAIR),
    }),
  );
  const bucketed = bucketEdges(edges);
  return {
    run_id: served.run_id,
    seed_account_key: served.seed_account_key,
    nodes,
    edges,
    truncated: served.truncated,
    truncation_reason: served.truncation_reason,
    cap: served.node_cap,
    collapsed_communities: served.collapsed_communities,
    window: { from: served.window_start, to: served.window_end },
    counterparty_note: served.counterparty_note,
    unbucketed_edges: bucketed.unbucketed,
    edges_by_bucket: bucketed.buckets,
    hops: served.hops,
    overlays: overlayCounts(nodes),
  };
}

export const SubgraphDecoder: Decoder<Subgraph> = mapDecode(ServedNetworkSubgraphDecoder, deriveSubgraph, 'Subgraph');

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

/**
 * `GET /api/meta/dataset` — `DatasetMeta` in `apps/api/schemas/catalog.py:77-91`, built
 * by `apps/api/routers/meta.py:130-160` out of `config/sources.yaml`.
 *
 * The card is per-source, not one object: two corpora, each with its own licence,
 * obligation, citation, files and label caveat, plus the sources the project declared
 * and refused, and the measurements with the command that produced each one. The client
 * used to decode a single-source card (`name / url / licence / rows / period /
 * class_balance / splits`), none of which the route has ever sent, so `/model`'s dataset
 * pane could only ever refuse. `row_count` and `size_bytes` are nullable on the wire —
 * a file the run never counted is served as null, and the pane says so rather than
 * printing 0 rows.
 */
export type DatasetFileCard = {
  file_name: string;
  sha256: string;
  size_bytes: number | null;
  row_count: number | null;
  verified_at: string | null;
};
const DatasetFileCardDecoder: Decoder<DatasetFileCard> = object('DatasetFileCard', {
  file_name: string,
  sha256: string,
  size_bytes: nullable(integer),
  row_count: nullable(integer),
  verified_at: nullable(TimestampDecoder),
});

export type DatasetSourceCard = {
  source_id: string;
  name: string;
  role: string;
  module: string | null;
  source_url: string;
  retrieval: string;
  /** `license`, American-spelled on the wire (`DatasetSourceCard.license`). */
  license: string;
  license_obligation: string;
  citation: string;
  description: string;
  label_caveat: string;
  known_biases: string[];
  synthetic_fields: string[];
  ingest_allowed: boolean;
  retrieved_at: string | null;
  files: DatasetFileCard[];
};
const DatasetSourceCardDecoder: Decoder<DatasetSourceCard> = object('DatasetSourceCard', {
  source_id: string,
  name: string,
  role: string,
  module: nullable(string),
  source_url: string,
  retrieval: string,
  license: string,
  license_obligation: string,
  citation: string,
  description: string,
  label_caveat: string,
  known_biases: array(string),
  synthetic_fields: array(string),
  ingest_allowed: boolean,
  retrieved_at: nullable(string),
  files: array(DatasetFileCardDecoder),
});

/** `MeasurementCard` — catalog.py:64-74: a number about the corpus plus the command
 *  that measured it. A statistic with no command is not served, so the client never has
 *  to decide what to do without one. */
export type MeasurementCard = {
  scope: string;
  name: string;
  value: number;
  unit: string | null;
  command: string;
  measured_at: string;
};
const MeasurementCardDecoder: Decoder<MeasurementCard> = object('MeasurementCard', {
  scope: string,
  name: string,
  value: number,
  unit: nullable(string),
  command: string,
  measured_at: TimestampDecoder,
});

export type RefusedSource = { source_id: string; name: string; reason: string; status: string };
const RefusedSourceDecoder: Decoder<RefusedSource> = object('RefusedSource', {
  source_id: string,
  name: string,
  reason: string,
  status: string,
});

export type DatasetCard = {
  sources: DatasetSourceCard[];
  refused_sources: RefusedSource[];
  measurements: MeasurementCard[];
  /** `config/pipeline.yaml`'s `sampling` section, verbatim. Typed as a scalar bag
   *  because the model types it `dict[str, Any]`: the route forwards the file, so the
   *  client may read what is there but may not require a key the config may drop.
   *  Booleans are admitted because YAML has them and the bag does not forbid them. */
  sampling: Record<string, string | number | boolean>;
  deidentification: {
    account_key: Record<string, string | number | boolean>;
    raw_identifier_policy: Record<string, string>;
    note: string;
  };
  disclaimer: string;
};

export const DatasetCardDecoder: Decoder<DatasetCard> = object('DatasetMeta', {
  sources: array(DatasetSourceCardDecoder),
  refused_sources: array(RefusedSourceDecoder),
  measurements: array(MeasurementCardDecoder),
  sampling: record(scalarInBag),
  deidentification: object('Deidentification', {
    account_key: record(scalarInBag),
    raw_identifier_policy: record(string),
    note: string,
  }),
  disclaimer: string,
});

/**
 * `GET /api/validation` — `ValidationBundle` in `apps/api/schemas/validation.py:231-254`,
 * built by `apps/api/routers/validation.py:128-174`.
 *
 * THE BUNDLE IS THE CONTRACT. This section used to decode fourteen names the route has
 * never sent (`baseline_table`, `pr_curve`, `operating_point`, `reliability`, `brier`,
 * `calibration_floor`, `shap_importance`, `drawdown`, `risk_adjusted`, `seeds`,
 * `configurations_evaluated`, `test_touched_at`, `degraded_dependencies`,
 * `entity_disjoint_note`), so `/api/validation` answered 200 with a body the decoder
 * refused and every one of `/model`'s thirteen panes sat in its skeleton forever. What
 * follows mirrors the Pydantic models field for field; the panes render what is served
 * and name, in `apps/web/CONTRACT-GAPS.md`, the UI concepts the server does not serve.
 *
 * Three of the served nulls are the point of the shapes, so they stay nullable:
 * `ConfusionMatrixView.budget` (a matrix at an unnamed budget is not at a budget),
 * `FoldRow.precision_at_budget` with `precision_undefined` and `precision_note` (an
 * undefined statistic is never 0 or 1), and `CurveSeries.note` (a one-point series is
 * explained rather than plotted as a trend).
 */

/** `FoldRow` — validation.py:43-89. One expanding-window walk-forward fold. */
export type ValidationFold = {
  fold_index: number;
  corpus: string;
  train_start: string;
  train_end: string;
  embargo_days: number;
  embargo_end: string;
  test_start: string;
  test_end: string;
  n_train: number;
  n_test: number;
  pr_auc: number;
  auroc: number;
  brier: number;
  precision_at_budget: number | null;
  recall_at_budget: number | null;
  precision_undefined: boolean;
  precision_note: string | null;
  alerts: number;
  captured_value: Money;
  cost: Money;
  net_benefit: Money;
  max_drawdown: Money;
  zero_drawdown_note: string | null;
  var95: Money;
  es975: Money;
  monte_carlo_runs: number;
  monte_carlo_seed: number;
  entity_disjoint: boolean;
  test_fold_touched_at: string | null;
};

const ValidationFoldDecoder: Decoder<ValidationFold> = object('FoldRow', {
  fold_index: integer,
  corpus: string,
  train_start: TimestampDecoder,
  train_end: TimestampDecoder,
  embargo_days: number,
  embargo_end: TimestampDecoder,
  test_start: TimestampDecoder,
  test_end: TimestampDecoder,
  n_train: integer,
  n_test: integer,
  pr_auc: number,
  auroc: number,
  brier: number,
  precision_at_budget: nullable(number),
  recall_at_budget: nullable(number),
  precision_undefined: boolean,
  precision_note: nullable(string),
  alerts: integer,
  captured_value: MoneyDecoder,
  cost: MoneyDecoder,
  net_benefit: MoneyDecoder,
  max_drawdown: MoneyDecoder,
  zero_drawdown_note: nullable(string),
  var95: MoneyDecoder,
  es975: MoneyDecoder,
  monte_carlo_runs: integer,
  monte_carlo_seed: integer,
  entity_disjoint: boolean,
  test_fold_touched_at: nullable(TimestampDecoder),
});

/** `AblationRowView` — validation.py:92-124. The CI arrives as its two bounds plus the
 *  method and the resample count, never as a `number[]` whose length is guessed. */
export type AblationRow = {
  variant: string;
  question: string;
  corpus: string;
  pr_auc: number;
  net_benefit: Money;
  ci_low: number;
  ci_high: number;
  ci_method: string;
  n_resamples: number;
  seed: number;
  is_leakage_control: boolean;
  is_graph_thesis: boolean;
  is_pricing_thesis: boolean;
};

const AblationRowDecoder: Decoder<AblationRow> = object('AblationRowView', {
  variant: string,
  question: string,
  corpus: string,
  pr_auc: number,
  net_benefit: MoneyDecoder,
  ci_low: number,
  ci_high: number,
  ci_method: string,
  n_resamples: integer,
  seed: integer,
  is_leakage_control: boolean,
  is_graph_thesis: boolean,
  is_pricing_thesis: boolean,
});

/** `CurveFamily` — validation.py:34-40, the five families the route will name. */
export const CurveFamilyDecoder = oneOf(
  'CurveFamily',
  'reliability',
  'pr_curve',
  'shap_global',
  'typology_recall',
  'per_typology_precision',
);
export type CurveFamily = 'reliability' | 'pr_curve' | 'shap_global' | 'typology_recall' | 'per_typology_precision';

/** `CurveDatum` — validation.py:127-135. */
export type CurveDatum = {
  point_index: number;
  x: number;
  y: number;
  n: number | null;
  label: string | null;
  operating_point: boolean;
};
const CurveDatumDecoder: Decoder<CurveDatum> = object('CurveDatum', {
  point_index: integer,
  x: number,
  y: number,
  n: nullable(integer),
  label: nullable(string),
  operating_point: boolean,
});

/** `CurveSeries` — validation.py:138-154, with the axis meanings the page prints. */
export type CurveSeries = {
  family: CurveFamily;
  x_label: string;
  y_label: string;
  points: CurveDatum[];
  currency: string | null;
  operating_threshold: number | null;
  note: string | null;
};
const CurveSeriesDecoder: Decoder<CurveSeries> = object('CurveSeries', {
  family: CurveFamilyDecoder,
  x_label: string,
  y_label: string,
  points: array(CurveDatumDecoder),
  currency: nullable(string),
  operating_threshold: nullable(number),
  note: nullable(string),
});

/** `ConfusionMatrixView` — validation.py:157-182. `budget` is nullable by design and
 *  `null` has its own sentence in the pane. */
export type ConfusionCell = { label: string; prediction: string; n: number };
const ConfusionCellDecoder: Decoder<ConfusionCell> = object('ConfusionCellView', {
  label: string,
  prediction: string,
  n: integer,
});
export type ConfusionMatrix = { cells: ConfusionCell[]; budget: number | null; basis: string };
const ConfusionMatrixDecoder: Decoder<ConfusionMatrix> = object('ConfusionMatrixView', {
  cells: array(ConfusionCellDecoder),
  budget: nullable(integer),
  basis: string,
});

/** `FairnessAxisView` / `FairnessRowView` — validation.py:185-206. */
export type FairnessRow = { bucket: string; fp_rate: number; fn_rate: number | null; n: number };
const FairnessRowDecoder: Decoder<FairnessRow> = object('FairnessRowView', {
  bucket: string,
  fp_rate: number,
  fn_rate: nullable(number),
  n: integer,
});
export type FairnessAxis = { axis: string; axis_rationale: string; rows: FairnessRow[] };
const FairnessAxisDecoder: Decoder<FairnessAxis> = object('FairnessAxisView', {
  axis: string,
  axis_rationale: string,
  rows: array(FairnessRowDecoder),
});

/** `PerturbationRowView` — validation.py:209-217. */
export type PerturbationRow = {
  kind: string;
  magnitude: number;
  result: number;
  unit: string;
  note: string;
  seed: number;
};
const PerturbationRowDecoder: Decoder<PerturbationRow> = object('PerturbationRowView', {
  kind: string,
  magnitude: number,
  result: number,
  unit: string,
  note: string,
  seed: integer,
});

/** `ValidationMetricView` — validation.py:220-228. Both `metrics` and `typology_recall`
 *  in the bundle are lists of these. */
export type ValidationMetric = {
  name: string;
  value: number;
  unit: string | null;
  corpus: string;
  note: string | null;
  n: number | null;
};
export const ValidationMetricDecoder: Decoder<ValidationMetric> = object('ValidationMetricView', {
  name: string,
  value: number,
  unit: nullable(string),
  corpus: string,
  note: nullable(string),
  n: nullable(integer),
});

/** `ValidationBundle.overfitting`, as `routers/validation.py:_overfitting` (:412-424)
 *  builds it: the `caveat` and the `keys` glossary are always present, the three metric
 *  numbers only when the run stored the metric. An absent number is null here and the
 *  pane says "not stored" rather than showing 0 configurations tried. */
export type Overfitting = {
  configurations_evaluated: number | null;
  test_fold_touched_once: number | null;
  selection_on_validation: number | null;
  caveat: string;
  keys: Record<string, string>;
};
const OverfittingDecoder: Decoder<Overfitting> = object('overfitting', {
  configurations_evaluated: nullable(number),
  test_fold_touched_once: nullable(number),
  selection_on_validation: nullable(number),
  caveat: string,
  keys: record(string),
});

/** `ValidationBundle.label_quality`, as `_label_quality` (:427-447) builds it: one
 *  entry per stored label metric, plus a `note` that is always present. The metric names
 *  are the dynamic keys, so they decode into a list and keep the name they arrived under
 *  rather than being dropped for not being in a fixed field map. */
export type LabelQualityEntry = { name: string; value: number; meaning: string; corpus: string };
export type LabelQuality = { entries: LabelQualityEntry[]; note: string };
const LabelQualityEntryDecoder: Decoder<{ value: number; meaning: string; corpus: string }> = object(
  'LabelQualityEntry',
  { value: number, meaning: string, corpus: string },
);
const LabelQualityDecoder: Decoder<LabelQuality> = {
  kind: 'label_quality',
  decode(value, path) {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      return { ok: false, error: { path, message: `expected label_quality object, got ${String(typeof value)}` } };
    }
    const record = value as Record<string, unknown>;
    const note = string.decode(record.note, `${path}.note`);
    if (!note.ok) return note;
    const entries: LabelQualityEntry[] = [];
    for (const key of Object.keys(record)) {
      if (key === 'note') continue;
      const entry = LabelQualityEntryDecoder.decode(record[key], `${path}.${key}`);
      if (!entry.ok) return entry;
      entries.push({ name: key, ...entry.value });
    }
    return { ok: true, value: { entries, note: note.value } };
  },
};

export type Validation = {
  run_id: string;
  corpora: string[];
  folds: ValidationFold[];
  ablation: AblationRow[];
  curves: CurveSeries[];
  confusion: ConfusionMatrix | null;
  fairness: FairnessAxis[];
  perturbations: PerturbationRow[];
  metrics: ValidationMetric[];
  typology_recall: ValidationMetric[];
  overfitting: Overfitting;
  label_quality: LabelQuality;
  limitations: string[];
  assumptions: AssumptionLine[];
};

export const ValidationDecoder: Decoder<Validation> = object('ValidationBundle', {
  run_id: string,
  corpora: array(string),
  folds: array(ValidationFoldDecoder),
  ablation: array(AblationRowDecoder),
  curves: array(CurveSeriesDecoder),
  confusion: nullable(ConfusionMatrixDecoder),
  fairness: array(FairnessAxisDecoder),
  perturbations: array(PerturbationRowDecoder),
  metrics: array(ValidationMetricDecoder),
  typology_recall: array(ValidationMetricDecoder),
  overfitting: OverfittingDecoder,
  label_quality: LabelQualityDecoder,
  limitations: array(string),
  assumptions: array(AssumptionLineDecoder),
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
      'P8a-3 workspace; reconciled. Decodes CaseDetail and every nested model it names, ' +
      'then derives the payload the page renders in deriveCasePayload (the same ' +
      'mapDecode pattern section 4 establishes for the explorer). The fields the route has ' +
      'never served — narrative, SHAP labels, evidence amounts, per-transaction rule codes, ' +
      'fusion coefficients, monte-carlo depth, counterfactual EV — are in CONTRACT-GAPS.md ' +
      'rather than defaulted here',
  },
  {
    route: 'POST /api/cases/{case_id}/decisions + POST /api/decisions/{decision_id}/confirm',
    note:
      'P8a-3 writes; body is DecisionCreate, receipt is DecisionWriteResult, and a 409 carries ' +
      'expected_version/current_version/current for the merge view (all three mirrored, not invented)',
  },
  {
    route: 'GET /api/graph/subgraph',
    note:
      'P8b-4 explorer; decodes NetworkSubgraph verbatim, then derives hopDepths / ' +
      'edges-by-UTC-day / overlay counts in lib/network/derive.ts (the route computes ' +
      '_hop_depths internally and does not emit it)',
  },
  {
    route: 'GET /api/scorecard + /drift + /disagreement',
    note: 'P8b-5 studio; scaling constants and the formula string',
  },
  { route: 'GET /api/policy + POST /api/policy/allocate', note: 'P8b-6 simulator; real re-allocation, no theatre' },
  { route: 'GET /api/validation', note: 'P8b-7 every chart served as data, not a screenshot' },
  { route: 'GET /api/meta/run', note: 'deployment_timezone + economics.yaml rendered verbatim' },
  { route: 'GET /api/runs/{run_id}/events', note: 'SSE stage ledger; Last-Event-ID resume' },
];
