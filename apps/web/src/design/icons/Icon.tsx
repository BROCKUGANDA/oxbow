/* =============================================================================
   OXBOW Icon component.
   Spec 12.6 PIPELINE: <Icon name="cycle" size={16} /> renders
   <use href="/sprite.svg#i-cycle" />.

   Sixty glyphs cost one request and zero per-icon JavaScript. Every glyph in the
   sprite is stroked with currentColor, so band colour drives glyph colour for
   free: putting a cycle glyph inside a band-C cell needs no second prop.

   The glyph name union below is the contract with sprite.svg. build.mjs emits
   the ids; this file is what makes a typo in a name a compile error rather than
   an empty box at runtime.
   ============================================================================= */

import type { CSSProperties, ReactElement } from 'react';

/**
 * Every glyph in the sprite. The twelve typology glyphs encode behaviour no icon
 * library carries - they cannot be downloaded, only drawn - and the three utility
 * glyphs are here so the app never reaches for Lucide.
 */
export type GlyphName =
  /* typologies */
  | 'cycle'
  | 'fan-in'
  | 'fan-out'
  | 'pass-through'
  | 'structuring'
  | 'velocity-spike'
  | 'dormant-wake'
  | 'fast-cash-out'
  | 'chain'
  | 'band-meter'
  | 'hash-link'
  | 'embargo'
  /* utility */
  | 'search'
  | 'filter'
  | 'chevron-down';

export const GLYPH_NAMES: readonly GlyphName[] = [
  'band-meter',
  'chain',
  'chevron-down',
  'cycle',
  'dormant-wake',
  'embargo',
  'fan-in',
  'fan-out',
  'fast-cash-out',
  'filter',
  'hash-link',
  'pass-through',
  'search',
  'structuring',
  'velocity-spike',
] as const;

/** The rule each typology glyph stands for, for chips and screen-reader labels. */
export const GLYPH_RULE: Readonly<Record<string, string>> = {
  'pass-through': 'R1 RAPID_PASS_THROUGH',
  'fan-in': 'R2 FAN_IN',
  'fan-out': 'R3 FAN_OUT',
  cycle: 'R4 CYCLE_MEMBER',
  structuring: 'R5 STRUCTURING',
  'velocity-spike': 'R6 VELOCITY_SPIKE',
  'dormant-wake': 'R7 DORMANT_REACTIVATION',
  'fast-cash-out': 'R10 FAST_CASH_OUT',
  chain: 'R12 CHAIN_MEMBER',
  embargo: 'walk-forward embargo',
  'hash-link': 'audit hash chain',
  'band-meter': 'risk band meter',
  search: 'search',
  filter: 'filter',
  'chevron-down': 'expand',
} as const;

/** Where the sprite is served from. The build emits it to the web public root. */
const SPRITE_HREF = '/sprite.svg';

export interface IconProps {
  /** Which glyph. Typed against the sprite, so an unknown name will not compile. */
  name: GlyphName;
  /** Rendered box in px. The 24px grid scales cleanly from 12 to 48. */
  size?: number;
  /**
   * Accessible name. When omitted the glyph is decorative and hidden from the
   * assistive tree, which is the right default for an icon sitting beside the
   * label it repeats.
   */
  title?: string;
  className?: string;
  style?: CSSProperties;
  /**
   * Band level 1-5. Only meaningful for band-meter, where it lights the first N
   * of the five segments. This is the redundant channel that keeps risk legible
   * in greyscale and under colour-vision deficiency.
   */
  bandLevel?: 1 | 2 | 3 | 4 | 5;
  /** Stroke width override. The set is 1.75; change this only to compensate for size. */
  strokeWidth?: number;
}

export function Icon({
  name,
  size = 16,
  title,
  className,
  style,
  bandLevel,
  strokeWidth,
}: IconProps): ReactElement {
  const decorative = title === undefined;
  const level = bandLevel ?? 1;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      role={decorative ? undefined : 'img'}
      aria-hidden={decorative ? true : undefined}
      aria-label={decorative ? undefined : title}
      focusable="false"
      strokeWidth={strokeWidth}
      style={{
        ...style,
        ...(bandLevel === undefined ? {} : ({ '--oxbow-band': String(level) } as CSSProperties)),
      }}
    >
      {decorative ? null : <title>{title}</title>}
      <use href={`${SPRITE_HREF}#i-${name}`} />
    </svg>
  );
}

export default Icon;
