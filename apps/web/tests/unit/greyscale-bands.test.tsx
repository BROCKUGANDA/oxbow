/**
 * §14 `test_greyscale_bands_distinguishable` — the band letters and the five-segment
 * meter glyph always render, and the colour ramp is built so a greyscale print keeps
 * the order: all five band tokens share one lightness, so luminance is carried by the
 * meter, never by the hue.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { BandBadge } from '@/components/ui/BandBadge';
import { bandMeterSegments, tokens } from '@/design/tokens';
import type { Band } from '@/lib/api/contract';
import { cleanupAll, mustFindAll, render } from '@/test/render';

afterEach(cleanupAll);

const BANDS: readonly Band[] = ['A', 'B', 'C', 'D', 'E'];

describe('band identity beyond colour', () => {
  it('every band renders its letter as printed text and its meter glyph', () => {
    const view = render(
      <div>
        {BANDS.map((band) => (
          <BandBadge key={band} band={band} />
        ))}
      </div>,
    );
    const badges = mustFindAll<HTMLSpanElement>(view.container, '[data-band]');
    expect(badges).toHaveLength(5);
    for (const badge of badges) {
      const band = badge.getAttribute('data-band') as Band;
      expect(badge.textContent).toContain(band);
      const meter = badge.querySelector('svg');
      expect(meter).not.toBeNull();
      expect(meter?.getAttribute('style') ?? '').toContain(`--oxbow-band: ${String(bandMeterSegments(band))}`);
    }
    view.cleanup();
  });

  it('the meter fills by ordinal: A=1 … E=5, and the fill always renders', () => {
    const levels = BANDS.map((band) => bandMeterSegments(band));
    expect(levels).toEqual([1, 2, 3, 4, 5]);
  });

  it('greyscale-safe by construction: every band colour carries the same lightness', () => {
    const colours = BANDS.map((band) => tokens.colours[`band_${band.toLowerCase()}` as keyof typeof tokens.colours]);
    const lightnesses = colours.map((colour: string) => {
      const match = /^oklch\(([\d.]+)\s/.exec(colour);
      if (match === null) throw new Error(`unexpected band colour token: ${colour}`);
      return Number(match[1]);
    });
    expect(new Set(lightnesses).size).toBe(1);
  });

  it('the print stylesheet restates the ramp and hides the sweep for the packet', () => {
    const css = readFileSync(path.join(process.cwd(), 'src/app/globals.css'), 'utf8');
    expect(css).toMatch(/@media print/);
    expect(css).toMatch(/data-print-hide/);
  });
});
