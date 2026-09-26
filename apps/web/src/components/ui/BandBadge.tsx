/* =============================================================================
   BandBadge — the rule that risk is never encoded by colour alone, as a component.

   DESIGN.md §3 bans it and §6 states the replacement: the letter AND the
   five-segment `band-meter` glyph always accompany the colour. Both channels are
   mandatory here rather than optional props, because an optional accessibility
   channel is one deadline away from being turned off — and the packet this product
   exports is laser-printed in greyscale.

   The meter is the P0 sprite glyph filled to `bandMeterSegments(band)`. Colour comes
   from the band token, so a theme change moves both channels together.
   ============================================================================= */

import type { ReactElement } from 'react';

import { Icon } from '../../design/icons/Icon';
import { bandMeterSegments, type Band as BandLetter } from '../../design/tokens';
import type { Band } from '../../lib/api/contract';

export type BandBadgeProps = {
  band: BandLetter | Band;
  /** Adds the word "band" for a screen reader; the letter alone is not a label. */
  describe?: boolean;
  size?: number;
};

const BAND_WORD: Record<BandLetter, string> = {
  A: 'band A, lowest risk',
  B: 'band B, low risk',
  C: 'band C, elevated risk',
  D: 'band D, high risk',
  E: 'band E, highest risk',
};

export function BandBadge({ band, describe = true, size = 14 }: BandBadgeProps): ReactElement {
  const level = bandMeterSegments(band) as 1 | 2 | 3 | 4 | 5;
  return (
    <span
      data-band={band}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        color: `var(--color-band-${band.toLowerCase()})`,
        whiteSpace: 'nowrap',
      }}
    >
      {/* Channel 1: the meter glyph. Its filled segment count is the ordinal. */}
      <Icon name="band-meter" size={size + 2} bandLevel={level} title={`${BAND_WORD[band]} meter`} />
      {/* Channel 2: the letter, always printed, never only in an aria label. */}
      <span className="u-tabular" style={{ fontFamily: 'var(--font-mono)', fontSize: size, fontWeight: 600 }}>
        {band}
      </span>
      {/* Channel 3: the words, for the assistive tree. */}
      {describe ? <span className="u-sr-only">{BAND_WORD[band]}</span> : null}
    </span>
  );
}

/** A band with its rate beside it, which is what a queue header needs. */
export function BandWithRate({ band, observedRate, n }: { band: Band; observedRate: number; n: number }): ReactElement {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <BandBadge band={band} describe={false} />
      <span className="u-tabular" style={{ fontSize: 'var(--text-micro)', color: 'var(--color-ink-muted)' }}>
        observed {(observedRate * 100).toFixed(1)}% · n={n.toLocaleString('en-US')}
      </span>
    </span>
  );
}
