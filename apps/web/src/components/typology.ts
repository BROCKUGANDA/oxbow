/* =============================================================================
   Typology naming: rule id -> glyph, code, and the plain words for it.

   This is vocabulary, not data. The numbers beside a typology — severity, hit rate,
   thresholds — come from the API; what lives here is which of the twelve hand-drawn
   glyphs stands for which rule, because `Icon` keys off the glyph name and the
   response keys off `R1`…`R12`. The mapping is the same R1–R12 table plan §9 defines,
   and the parameter names in `config/rules.yaml` are surfaced from the response
   rather than typed here.
   ============================================================================= */

import type { GlyphName } from '../design/icons/Icon';
import type { Typology } from '../lib/api/contract';

export type TypologyMeta = {
  glyph: GlyphName;
  code: string;
  name: string;
  /** What the pattern *is*, in one sentence the analyst can read aloud. */
  reads: string;
};

export const TYPOLOGY_META: Record<Typology, TypologyMeta> = {
  R1: {
    glyph: 'pass-through',
    code: 'RAPID_PASS_THROUGH',
    name: 'Rapid pass-through',
    reads: 'receives, then forwards most of it on inside the window',
  },
  R2: { glyph: 'fan-in', code: 'FAN_IN', name: 'Fan-in', reads: 'gathers from many senders in small amounts' },
  R3: { glyph: 'fan-out', code: 'FAN_OUT', name: 'Fan-out', reads: 'scatters to many receivers' },
  R4: {
    glyph: 'cycle',
    code: 'CYCLE_MEMBER',
    name: 'Cycle member',
    reads: 'value leaves and comes back, time-respecting and value-retaining',
  },
  R5: {
    glyph: 'structuring',
    code: 'STRUCTURING',
    name: 'Structuring',
    reads: 'repeats just under a reporting threshold',
  },
  R6: {
    glyph: 'velocity-spike',
    code: 'VELOCITY_SPIKE',
    name: 'Velocity spike',
    reads: 'activity breaks against its own baseline',
  },
  R7: {
    glyph: 'dormant-wake',
    code: 'DORMANT_REACTIVATION',
    name: 'Dormant reactivation',
    reads: 'quiet for a long stretch, then busy inside a day',
  },
  R8: {
    glyph: 'velocity-spike',
    code: 'ODD_HOUR_SHIFT',
    name: 'Odd-hour shift',
    reads: 'volume moves into historically quiet hours',
  },
  R9: {
    glyph: 'structuring',
    code: 'AMOUNT_REGIME_SHIFT',
    name: 'Amount regime shift',
    reads: 'typical amount changes level',
  },
  R10: {
    glyph: 'fast-cash-out',
    code: 'FAST_CASH_OUT',
    name: 'Fast cash-out',
    reads: 'inflow leaves again as cash almost immediately',
  },
  R11: {
    glyph: 'fan-out',
    code: 'NEW_COUNTERPARTY_SURGE',
    name: 'New-counterparty surge',
    reads: 'most counterparties are first-time',
  },
  R12: {
    glyph: 'chain',
    code: 'CHAIN_MEMBER',
    name: 'Chain member',
    reads: 'a layering path of hops, each smaller than the last',
  },
};

export function glyphFor(typology: Typology | null): GlyphName {
  return typology === null ? 'chain' : TYPOLOGY_META[typology].glyph;
}

export function ruleCode(typology: Typology | null): string {
  return typology === null ? 'no single rule dominates' : TYPOLOGY_META[typology].code;
}

/** The band's implied action, from the scorecard band table on the response. */
export const BAND_ORDER: readonly Typology[] = [
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
];
