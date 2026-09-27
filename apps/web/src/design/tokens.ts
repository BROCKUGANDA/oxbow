// GENERATED FILE. DO NOT EDIT. Run: node apps/web/src/design/icons/build.mjs
// OXBOW design tokens, mirrored for Cytoscape, canvas and lightweight-charts.
// These renderers cannot read CSS custom properties, so the values are compiled
// here from tokens.css, with every paper-mode var() reference resolved to a
// literal colour. Risk is never encoded by colour alone: every band ships with
// bandMeterSegments() as well as its hue.

export const tokens = {
  colours: {
  band_a: 'oklch(0.78 0.075 235)',
  band_b: 'oklch(0.78 0.095 195)',
  band_c: 'oklch(0.78 0.115 155)',
  band_d: 'oklch(0.78 0.135 95)',
  band_e: 'oklch(0.78 0.155 40)',
  canvas: 'oklch(0.17 0.012 250)',
  canvas_raised: 'oklch(0.205 0.014 250)',
  canvas_sunken: 'oklch(0.142 0.011 250)',
  elev_1: 'oklch(0.205 0.014 250)',
  elev_2: 'oklch(0.245 0.016 250)',
  evidence: 'oklch(0.86 0.115 195)',
  focus: 'oklch(0.86 0.115 195)',
  hairline: 'oklch(0.86 0.01 250 / 0.08)',
  hairline_inset: 'oklch(0.1 0.01 250 / 0.4)',
  hairline_strong: 'oklch(0.86 0.01 250 / 0.16)',
  ink: 'oklch(0.96 0.006 250)',
  ink_disabled: 'oklch(0.42 0.012 250)',
  ink_faint: 'oklch(0.64 0.012 250)',
  ink_inverse: 'oklch(0.17 0.012 250)',
  ink_muted: 'oklch(0.74 0.01 250)',
  paper: 'oklch(0.98 0.004 90)',
  paper_raised: 'oklch(1 0 0)',
  paper_sunken: 'oklch(0.955 0.005 90)',
  state_done: 'oklch(0.78 0.1 150)',
  state_failed: 'oklch(0.68 0.17 25)',
  state_pending: 'oklch(0.44 0.01 250)',
  state_running: 'oklch(0.78 0.1 200)',
  state_skipped: 'oklch(0.5 0.01 250)',
  typo_r1_pass_through: 'oklch(0.74 0.09 25)',
  typo_r10_fast_cash_out: 'oklch(0.74 0.08 335)',
  typo_r11_counterparty_surge: 'oklch(0.74 0.085 12)',
  typo_r12_chain: 'oklch(0.74 0.09 45)',
  typo_r2_fan_in: 'oklch(0.74 0.1 60)',
  typo_r3_fan_out: 'oklch(0.74 0.105 95)',
  typo_r4_cycle: 'oklch(0.74 0.1 130)',
  typo_r5_structuring: 'oklch(0.74 0.09 165)',
  typo_r6_velocity_spike: 'oklch(0.74 0.085 200)',
  typo_r7_dormant_wake: 'oklch(0.74 0.08 235)',
  typo_r8_odd_hour_shift: 'oklch(0.74 0.075 268)',
  typo_r9_amount_regime_shift: 'oklch(0.74 0.07 300)',
  },
  paper: {

  },
  durations: {
  base: '220ms',
  deliberate: '520ms',
  fast: '140ms',
  instant: '80ms',
  slow: '320ms',
  },
  easings: {
  in_quad: 'cubic-bezier(0.55, 0.085, 0.68, 0.53)',
  out_quint: 'cubic-bezier(0.22, 1, 0.36, 1)',
  },
} as const;

export type TokenColour = keyof typeof tokens.colours;
export type Band = 'A' | 'B' | 'C' | 'D' | 'E';
export type TypologyId =
  | 'R1_RAPID_PASS_THROUGH'
  | 'R2_FAN_IN'
  | 'R3_FAN_OUT'
  | 'R4_CYCLE_MEMBER'
  | 'R5_STRUCTURING'
  | 'R6_VELOCITY_SPIKE'
  | 'R7_DORMANT_REACTIVATION'
  | 'R8_ODD_HOUR_SHIFT'
  | 'R9_AMOUNT_REGIME_SHIFT'
  | 'R10_FAST_CASH_OUT'
  | 'R11_NEW_COUNTERPARTY_SURGE'
  | 'R12_CHAIN_MEMBER';

/** The five band colours, named. Each sits at equal lightness; chroma carries severity. */
export const BAND_COLOURS = {
  A: 'oklch(0.78 0.075 235)',
  B: 'oklch(0.78 0.095 195)',
  C: 'oklch(0.78 0.115 155)',
  D: 'oklch(0.78 0.135 95)',
  E: 'oklch(0.78 0.155 40)',
} as const satisfies Record<Band, string>;

/** Semantic typology colours, keyed by rule id. Never a risk signal on its own. */
export const TYPOGRAPHY_COLOURS = {
  TYPO_R1_PASS_THROUGH: 'oklch(0.74 0.09 25)',
  TYPO_R10_FAST_CASH_OUT: 'oklch(0.74 0.08 335)',
  TYPO_R11_COUNTERPARTY_SURGE: 'oklch(0.74 0.085 12)',
  TYPO_R12_CHAIN: 'oklch(0.74 0.09 45)',
  TYPO_R2_FAN_IN: 'oklch(0.74 0.1 60)',
  TYPO_R3_FAN_OUT: 'oklch(0.74 0.105 95)',
  TYPO_R4_CYCLE: 'oklch(0.74 0.1 130)',
  TYPO_R5_STRUCTURING: 'oklch(0.74 0.09 165)',
  TYPO_R6_VELOCITY_SPIKE: 'oklch(0.74 0.085 200)',
  TYPO_R7_DORMANT_WAKE: 'oklch(0.74 0.08 235)',
  TYPO_R8_ODD_HOUR_SHIFT: 'oklch(0.74 0.075 268)',
  TYPO_R9_AMOUNT_REGIME_SHIFT: 'oklch(0.74 0.07 300)',
} as const;

/** The reserved accent. Its only job is marking what the thing you clicked is made of. */
export const EVIDENCE = 'oklch(0.86 0.115 195)' as const;

export const CANVAS = 'oklch(0.17 0.012 250)' as const;
export const CANVAS_RAISED = 'oklch(0.205 0.014 250)' as const;
export const HAIRLINE = 'oklch(0.86 0.01 250 / 0.08)' as const;
export const INK = 'oklch(0.96 0.006 250)' as const;
export const INK_MUTED = 'oklch(0.74 0.01 250)' as const;
export const INK_FAINT = 'oklch(0.64 0.012 250)' as const;

export const DURATIONS = {
  BASE: '220ms',
  DELIBERATE: '520ms',
  FAST: '140ms',
  INSTANT: '80ms',
  SLOW: '320ms',
} as const;

export const EASINGS = {
  IN_QUAD: 'cubic-bezier(0.55, 0.085, 0.68, 0.53)',
  OUT_QUINT: 'cubic-bezier(0.22, 1, 0.36, 1)',
} as const;

export type DurationToken = keyof typeof DURATIONS;
export type EasingToken = keyof typeof EASINGS;

const SEGMENTS_PER_BAND: Readonly<Record<Band, number>> = { A: 1, B: 2, C: 3, D: 4, E: 5 };

/**
 * The redundant channel that makes risk survivable without colour: how many of the
 * five meter segments are lit. A greyscale export packet keeps the band.
 */
export function bandMeterSegments(band: Band): number {
  return SEGMENTS_PER_BAND[band];
}

/** CSS custom property value for the band-meter glyph, which fills to the band. */
export function bandMeterVar(band: Band): string {
  return String(SEGMENTS_PER_BAND[band]);
}

export function bandColour(band: Band): string {
  return BAND_COLOURS[band];
}
