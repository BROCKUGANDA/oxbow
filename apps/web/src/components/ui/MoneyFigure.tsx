/* =============================================================================
   MoneyFigure — the only way a currency value reaches the screen.

   DESIGN.md §7 and plan §11 make this a hard rule, and a rule a component can skip
   is not a rule. So:

   * The renderer takes the `MoneyFigure` type, which is what the API sends: a value
     PLUS its recovery-rate band. There is no constructor from a bare number, so
     `figureFrom(412000000)` is not a thing a component can write.
   * It requires `assumptions` and throws through `requireAssumptions` when the
     payload carries none — which is what makes a missing assumptions block a test
     failure rather than a subtly unlabelled number.
   * It divides by 100 here and only here, via `fixedFromMinor`, which is integer
     long division rather than a float divide.

   The band is not a tooltip. It is rendered inline, three values, because the
   recovery rate is the assumption every money figure on this product depends on and
   hiding it behind a hover is how a demo day produces a number nobody can defend.
   ============================================================================= */

import type { ReactElement } from 'react';

import type { AssumptionLine, MoneyFigure as MoneyFigureValue } from '../../lib/api/contract';
import { assumptionLine, moneyAtBand, presentMoney, requireAssumptions } from '../../lib/format/money';
import { T_KPI, T_LABEL, T_MICRO } from './sx';

export type MoneyFigureProps = {
  figure: MoneyFigureValue;
  /** From `meta.assumptions`; empty or missing is a render-time failure by design. */
  assumptions: readonly AssumptionLine[];
  /** The label above the number, e.g. "Expected loss avoided this period". */
  label: string;
  /** Hero sizing is for the dashboard strip only. */
  emphasis?: 'kpi' | 'inline';
  /** Compact form for table cells and chips; full precision stays in the title. */
  compact?: boolean;
  /** Whether the recovery band is shown beside this instance. Default true. */
  showBand?: boolean;
  /** A footer note that names the config file, from the payload, not from here. */
  source?: string | null;
};

export function MoneyFigure({
  figure,
  assumptions,
  label,
  emphasis = 'inline',
  compact = false,
  showBand = true,
  source = null,
}: MoneyFigureProps): ReactElement {
  const lines = requireAssumptions(label, assumptions);
  const value = presentMoney(figure.value);
  const legs = moneyAtBand(figure);

  return (
    <figure
      data-money-figure={label}
      style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 2 }}
    >
      <figcaption style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</figcaption>

      <div
        className="u-tabular"
        style={{
          ...(emphasis === 'kpi' ? T_KPI : { fontFamily: 'var(--font-sans)', fontSize: 'var(--text-body)', fontWeight: 600 }),
          color: 'var(--color-ink)',
          display: 'flex',
          alignItems: 'baseline',
          gap: 4,
        }}
        title={`${value.exact} ${value.currency}`}
      >
        <span>{compact ? value.compact : value.exact}</span>
        <span style={{ ...T_LABEL, fontWeight: 500, color: 'var(--color-ink-muted)' }}>{value.currency}</span>
      </div>

      {showBand && legs.length > 0 ? (
        <div
          className="u-tabular"
          data-recovery-band
          style={{ ...T_MICRO, color: 'var(--color-ink-faint)', display: 'flex', gap: 6, flexWrap: 'wrap' }}
        >
          <span>recovery-rate band</span>
          {legs.map((leg) => (
            <span key={leg.rate} style={{ whiteSpace: 'nowrap' }}>
              r {leg.rate.toFixed(2)} → {leg.money.compact}
            </span>
          ))}
        </div>
      ) : null}

      {/* The assumption line. Naming the keys, because "it depends on assumptions"
          is the sentence that replaces them and proves nothing. */}
      <div data-assumption-line style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        assumes {assumptionLine(lines)}
        {source !== null ? ` · ${source}` : ''}
      </div>
    </figure>
  );
}

/** A money value inside a table cell: compact, with the band in the title attribute. */
export function MoneyCell({
  figure,
  assumptions,
  label,
}: {
  figure: MoneyFigureValue;
  assumptions: readonly AssumptionLine[];
  label: string;
}): ReactElement {
  return <MoneyFigure figure={figure} assumptions={assumptions} label={label} compact showBand={false} />;
}
