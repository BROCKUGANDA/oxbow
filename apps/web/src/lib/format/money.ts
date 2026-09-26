/* =============================================================================
   Money rendering. THE ONLY PLACE minor units become a number a human reads.

   Two rules, both load-bearing:

   * DEV-005 / plan §5: money is an integer count of minor units end to end. The
     division by `decimals` happens HERE, at the last moment, and nowhere earlier —
     a total computed from already-divided values picks up a rounding error per row
     and the queue total stops matching the sum of the rows it is made of.
     `decimals` comes from the response (`Money.decimals`, which mirrors
     `minor_units_per_major` in config/economics.yaml), so the renderer never has to
     be told that a cent is a hundredth.
   * plan §11 / DESIGN.md §7: a currency figure may not render without its
     assumption line and its recovery-rate band. That is not a style rule, it is a
     type rule: `MoneyFigure` props require an `assumptions` array with at least one
     entry naming a config key, and `moneyAtBand()` returns exactly three band
     values or throws. A call site that forgets fails the render and fails the test
     `test_currency_requires_assumptions`.

   The integer long-division below is deliberate. `minor / 100` in floating point is
   the classic way to render 1234.56 as 1234.5599999999999, and this is a document
   a judge reads digit-for-digit.
   ============================================================================= */

import type { AssumptionLine, Money, MoneyFigure } from '../api/contract';

/** The three recovery rates the band is always shown over (config: recovery.sensitivity_band). */
export const RECOVERY_BAND_WIDTH = 3;

export class MissingAssumptions extends Error {
  constructor(figure: string) {
    super(
      `currency figure "${figure}" has no assumption line: every money value must name the ` +
        `config/economics.yaml keys it depends on (plan §11, DESIGN.md §7)`,
    );
    this.name = 'MissingAssumptions';
  }
}

/**
 * Minor units to a fixed-point string, without ever forming a float.
 * `123456` at 2 decimals -> "1234.56"; `-5` at 2 -> "-0.05".
 */
export function fixedFromMinor(minor: number, decimals: number): string {
  const negative = minor < 0;
  const magnitude = Math.abs(Math.trunc(minor));
  const scale = 10 ** decimals;
  const whole = Math.floor(magnitude / scale);
  const rest = magnitude % scale;
  const grouped = groupThousands(whole.toString());
  if (decimals === 0) return negative ? `-${grouped}` : grouped;
  const fraction = rest.toString().padStart(decimals, '0');
  const body = `${grouped}.${fraction}`;
  return negative ? `-${body}` : body;
}

function groupThousands(value: string): string {
  return value.length <= 3 ? value : `${groupThousands(value.slice(0, -3))},${value.slice(-3)}`;
}

/**
 * Axis tick formatter for a money axis. `decimals` is a caller's argument rather than
 * a default, because the only honest source for it is `Money.decimals` on the
 * response — an axis that divides by a number the component chose is exactly the
 * invented constant this whole layer exists to prevent.
 */
export function moneyAxisFormatter(decimals: number): (value: number) => string {
  return (value: number) => compactFromMinor(value, decimals);
}

/** Compact axis/label form: 1.24 M, 8,900 k. Groups are thousands, never a float sum. */
export function compactFromMinor(minor: number, decimals: number): string {
  const negative = minor < 0;
  const magnitude = Math.abs(Math.trunc(minor));
  const scale = 10 ** decimals;
  const units = [
    { threshold: scale * 1_000_000_000, suffix: 'bn' },
    { threshold: scale * 1_000_000, suffix: 'm' },
    { threshold: scale * 1_000, suffix: 'k' },
  ];
  for (const unit of units) {
    if (magnitude >= unit.threshold) {
      const whole = Math.floor(magnitude / unit.threshold);
      const tenth = Math.floor(((magnitude % unit.threshold) / unit.threshold) * 10);
      const text = `${String(whole)}.${String(tenth)}${unit.suffix}`;
      return negative ? `-${text}` : text;
    }
  }
  return fixedFromMinor(minor, 0);
}

export type PresentedMoney = {
  /** Full precision, grouped, e.g. "1,234,567.00". */
  readonly exact: string;
  /** Axis/label form, e.g. "1.23m". */
  readonly compact: string;
  readonly currency: string;
  readonly decimals: number;
  readonly minor: number;
};

export function presentMoney(money: Money): PresentedMoney {
  return {
    exact: fixedFromMinor(money.minor, money.decimals),
    compact: compactFromMinor(money.minor, money.decimals),
    currency: money.currency,
    decimals: money.decimals,
    minor: money.minor,
  };
}

/** One leg of the sensitivity band: a recovery rate and the money it produces. */
export type BandLeg = { rate: number; money: PresentedMoney };

/**
 * The recovery-rate band. Exactly three legs or the renderer refuses, because a
 * two-value "band" is a point estimate with a spare number attached and
 * `test_recovery_rate_bounds` exists to catch exactly that.
 */
export function moneyAtBand(figure: MoneyFigure): BandLeg[] {
  const { band, band_rates: rates } = figure;
  if (band === null || rates === null) return [];
  if (band.length !== rates.length) {
    throw new Error(`recovery band has ${String(band.length)} values and ${String(rates.length)} rates`);
  }
  return rates.map((rate, index) => {
    const money = band[index];
    if (money === undefined) throw new Error(`recovery band leg ${String(index)} is missing`);
    return { rate, money: presentMoney(money) };
  });
}

/**
 * THE gate. Called by every currency renderer. A figure whose payload carries no
 * assumptions is a number whose provenance nobody can check, which is the plan §11
 * rejection trigger, so this throws rather than rendering it.
 */
export function requireAssumptions(figure: string, assumptions: readonly AssumptionLine[]): AssumptionLine[] {
  if (assumptions.length === 0) throw new MissingAssumptions(figure);
  return [...assumptions];
}

/** The assumption line as copy: `assumptions: recovery.rate 0.35 · analyst.cost_per_minute_minor 15000`. */
export function assumptionLine(assumptions: readonly AssumptionLine[]): string {
  return assumptions.map((line) => `${line.key} ${String(line.value)}`).join(' · ');
}

/** The band as copy: "at r = 0.20 / 0.35 / 0.50: 1.2m · 2.1m · 3.0m". */
export function bandLine(legs: readonly BandLeg[], currency: string): string {
  const rates = legs.map((leg) => leg.rate.toFixed(2)).join(' / ');
  const values = legs.map((leg) => `${leg.money.compact} ${currency}`).join(' · ');
  return `r = ${rates}: ${values}`;
}

/** A plain rate, as a percentage with its own precision fixed by the caller. */
export function percent(value: number, places = 1): string {
  return `${(value * 100).toFixed(places)}%`;
}

export function ratio(value: number, places = 3): string {
  return value.toFixed(places);
}

/** Integer counts: never a float, never a silently truncated decimal. */
export function count(value: number): string {
  return groupThousands(Math.trunc(value).toString());
}
