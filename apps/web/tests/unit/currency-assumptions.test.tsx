/**
 * Plan §11 / DESIGN.md §7, the mechanism behind the §18 rejection trigger "a number
 * in the DOM that appears in no response": a currency figure that reaches the screen
 * without its assumptions block is a render-time failure, not a subtly unlabelled
 * number. `test_currency_requires_assumptions` and the recovery band's three legs.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { MoneyFigure } from '@/components/ui/MoneyFigure';
import type { AssumptionLine, MoneyFigure as MoneyFigureValue } from '@/lib/api/contract';
import { MissingAssumptions, fixedFromMinor, moneyAtBand, requireAssumptions } from '@/lib/format/money';
import { cleanupAll, mustFind, render, text } from '@/test/render';

afterEach(cleanupAll);

const ASSUMPTIONS: AssumptionLine[] = [
  { key: 'recovery.rate', value: 0.35, source: 'config/economics.yaml', note: 'illustrative' },
  { key: 'analyst.cost_per_minute_minor', value: 15000, source: 'config/economics.yaml', note: null },
];

function figure(minor: number): MoneyFigureValue {
  return {
    value: { minor, currency: 'UGX', decimals: 2 },
    band: [
      { minor: Math.round((minor * 0.2) / 0.35), currency: 'UGX', decimals: 2 },
      { minor: Math.round((minor * 0.35) / 0.35), currency: 'UGX', decimals: 2 },
      { minor: Math.round((minor * 0.5) / 0.35), currency: 'UGX', decimals: 2 },
    ],
    band_rates: [0.2, 0.35, 0.5],
  };
}

describe('a currency figure cannot render without its assumptions', () => {
  it('throws MissingAssumptions through the component, not just the helper', () => {
    // The throw happens during render — that is the point. React rethrows it out of
    // act(); the boundary-less mount must fail the app, which is the §11 gate.
    let thrown: unknown = null;
    try {
      render(<MoneyFigure figure={figure(412_000_000)} assumptions={[]} label="Loss avoided" />);
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(MissingAssumptions);
    expect((thrown as Error).message).toContain('has no assumption line');
  });

  it('requireAssumptions is the chokepoint: one entry, any config key, is enough', () => {
    expect(() => requireAssumptions('x', [])).toThrow(MissingAssumptions);
    expect(requireAssumptions('x', ASSUMPTIONS)).toHaveLength(2);
  });

  it('when it does render, the figure carries the band and the named assumption line', () => {
    const view = render(
      <MoneyFigure figure={figure(412_000_000)} assumptions={ASSUMPTIONS} label="Expected loss avoided" emphasis="kpi" />,
    );
    const body = text(view.container);
    // 412,000,000 minor units at 2 decimals is 4,120,000.00 UGX — the division the
    // renderer is the only place it happens.
    expect(body).toContain('4,120,000.00');
    expect(body).toContain('UGX');
    expect(body).toContain('recovery-rate band');
    expect(body).toContain('r 0.20');
    expect(body).toContain('r 0.50');
    const assumption = mustFind<HTMLElement>(view.container, '[data-assumption-line]');
    expect(assumption.textContent).toContain('recovery.rate 0.35');
    view.cleanup();
  });

  it('moneyAtBand refuses a two-value "band" — a point estimate with a spare number', () => {
    expect(() =>
      moneyAtBand({
        value: { minor: 100, currency: 'UGX', decimals: 2 },
        band: [{ minor: 50, currency: 'UGX', decimals: 2 }],
        band_rates: [0.2, 0.35],
      }),
    ).toThrowError();
    expect(moneyAtBand(figure(1000))).toHaveLength(3);
    expect(moneyAtBand({ value: { minor: 1, currency: 'UGX', decimals: 2 }, band: null, band_rates: null })).toEqual([]);
  });

  it('minor-unit division is exact long division, never a float artifact', () => {
    expect(fixedFromMinor(123456, 2)).toBe('1,234.56');
    expect(fixedFromMinor(-5, 2)).toBe('-0.05');
    expect(fixedFromMinor(1000000000, 2)).toBe('10,000,000.00');
    expect(fixedFromMinor(99, 0)).toBe('99');
  });
});
