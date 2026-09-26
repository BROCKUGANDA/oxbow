/* =============================================================================
   FIXTURE DATA — developer contract double. THIS FILE IS A TEST DOUBLE.

   Nothing in `src/fixtures/**` is a production module. It exists because P7's
   routes are being written in parallel and the screens have to be built against
   their shapes today. The fixture transport that serves it is unreachable in a
   production build (see `resolveMode` in lib/api/transport.ts) and stamps
   `provenance: 'fixture'` on every payload it returns, which the app shell renders
   as a permanent banner. A fixture therefore cannot arrive on a screen looking
   like a measurement.

   Every value here is hand-written or produced by the seeded generator below, and
   every one of them is shaped by the decoders in lib/api/contract.ts — so the
   fixture is also the executable statement of what the contract expects. When P7
   lands, a route that disagrees with these shapes fails the client's decode rather
   than rendering a blank.

   Money is UGX minor units (x100), matching config/economics.yaml.
   ============================================================================= */

import type { AssumptionLine, ListMeta, Money, MoneyFigure } from '../lib/api/contract';

export function money(minor: number, currency = 'UGX', decimals = 2): Money {
  return { minor, currency, decimals };
}

/**
 * A currency figure with its recovery-rate band.
 *
 * The band legs are the same exposure priced at r = 0.20 / 0.35 / 0.50 against the
 * 0.35 default, which is the sensitivity rule in config/economics.yaml. Real
 * numbers come from the server; the shape — three legs, three rates, always
 * together — is what the renderer requires and what `moneyAtBand` refuses to
 * tolerate otherwise.
 */
export function figure(minor: number, currency = 'UGX'): MoneyFigure {
  const rates = [0.2, 0.35, 0.5];
  return {
    value: money(minor, currency),
    band: rates.map((rate) => money(Math.round((minor * rate) / 0.35), currency)),
    band_rates: rates,
  };
}

/** The assumption lines every money figure in these fixtures carries. */
export const ASSUMPTIONS: AssumptionLine[] = [
  {
    key: 'recovery.rate',
    value: 0.35,
    source: 'config/economics.yaml',
    note: 'illustrative; the band is r in {0.20, 0.35, 0.50}',
  },
  { key: 'analyst.cost_per_minute_minor', value: 15000, source: 'config/economics.yaml', note: null },
  { key: 'friction_cost_minor', value: 2500000, source: 'config/economics.yaml', note: null },
  { key: 'exposure.window_hours', value: 24, source: 'config/economics.yaml', note: null },
];

export const DISCLAIMER =
  'OXBOW is a research prototype that analyzes historical, de-identified data only. It does not process live ' +
  'financial transactions, does not trade or advise on any financial instrument, does not make real financial ' +
  'decisions, and is not financial advice. Monetary figures are model estimates derived from stated assumptions, ' +
  'not measured outcomes. Results are not validated for operational use by any financial institution.';

/** The meta block as P7 sends it, with the fixture provenance that makes it honest. */
export function meta(overrides: Partial<ListMeta> = {}): ListMeta {
  return {
    run_id: overrides.run_id ?? '01JFixtureRun0000000000000',
    trace_id: overrides.trace_id ?? '01JFixtureTrace000000000000',
    model_version: overrides.model_version ?? 'lgbm-fusion-4.5.3+iso',
    provenance: 'fixture:developer-contract',
    generated_at: overrides.generated_at ?? '2026-09-25T06:12:00Z',
    assumptions: overrides.assumptions ?? ASSUMPTIONS,
    degraded: overrides.degraded ?? false,
    degraded_reason: overrides.degraded_reason ?? null,
    disclaimer: DISCLAIMER,
    limit: overrides.limit ?? null,
    offset: overrides.offset ?? null,
    total: overrides.total ?? null,
    next_offset: overrides.next_offset ?? null,
    sort: overrides.sort ?? null,
    order: overrides.order ?? null,
  };
}

/** The envelope, exactly as the wire sends it: two keys and nothing else. */
export function envelope<T>(data: T, overrides: Partial<ListMeta> = {}): { data: T; meta: ListMeta } {
  return { data, meta: meta(overrides) };
}

/* ------------------------------------------------------------- generators -- */

/**
 * A seeded LCG. Deterministic, so the virtualised 10k-row queue and the 1,500-node
 * graph are the same bytes on every run and a measured frame rate means something.
 */
export function seeded(seed: number): () => number {
  let state = seed % 2147483647;
  if (state <= 0) state += 2147483646;
  return () => {
    state = (state * 16807) % 2147483647;
    return (state - 1) / 2147483646;
  };
}

const BANDS = ['A', 'B', 'C', 'D', 'E'] as const;
const RULES = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10', 'R11', 'R12'] as const;

export function accountKey(next: () => number): string {
  const hex = Math.floor(next() * 0xffffff)
    .toString(16)
    .toUpperCase()
    .padStart(6, '0');
  return `ACC-${hex}`;
}

export function pick<T>(values: readonly T[], next: () => number): T {
  const index = Math.floor(next() * values.length) % values.length;
  return values[index] as T;
}

export const BAND_LETTERS = BANDS;
export const RULE_IDS = RULES;
