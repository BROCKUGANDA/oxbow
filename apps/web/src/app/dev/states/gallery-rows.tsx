/* =============================================================================
   Gallery rows that need their own state, so they live beside the gallery rather
   than in the design system: they are demonstrations of behaviour, not components the
   product renders.

   * `FigureRow` proves the money rule bites. It hands `MoneyFigure` a payload with no
     assumptions block, which throws at render; the boundary around it catches that and
     the row prints what the refusal was. If the plan §11 clause were only a comment,
     this row would render a number.
   * `LongTextRow` is `test_long_text_truncated_and_escaped`: markup-shaped text and a
     combining-character run, rendered as text, truncated with the full value in the
     title, and asserted to produce no element other than the one it was put in.
   * `StaleRow` is `test_stale_response_discarded`: two requests on the same key with
     the slower one started first, and the rendered answer is whichever the sequence
     guard accepts.
   ============================================================================= */

'use client';

import { useEffect, useState, type ReactElement } from 'react';
import { ErrorBoundary } from 'react-error-boundary';

import { begin, isStale } from '@/lib/api/client';
import type { MoneyFigure as MoneyFigureValue } from '@/lib/api/contract';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { MissingAssumptions } from '@/lib/format/money';
import { ELLIPSIS, PANEL_SUNKEN, T_LABEL, T_MICRO } from '@/components/ui/sx';

/** Deliberately empty: the point is that the renderer refuses. */
const NO_ASSUMPTIONS: MoneyFigureValue = {
  value: { minor: 412_000_000, currency: 'UGX', decimals: 2 },
  band: null,
  band_rates: null,
};

export function FigureRow(): ReactElement {
  return (
    <div style={{ ...PANEL_SUNKEN, padding: 12 }}>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '0 0 8px' }}>
        the same component, given a figure with no assumptions block
      </p>
      <ErrorBoundary
        fallbackRender={({ error }) => (
          <p role="alert" data-money-refused style={{ ...T_LABEL, color: 'var(--color-state-failed)', margin: 0 }}>
            {error instanceof MissingAssumptions ? error.message : 'the renderer refused this figure'}
          </p>
        )}
      >
        <MoneyFigure figure={NO_ASSUMPTIONS} assumptions={[]} label="Loss avoided, unlabelled" />
      </ErrorBoundary>
    </div>
  );
}

/** Markup-shaped input, rendered as text. React escapes it; the test asserts no node
 *  was created and the whole value survives in the title. */
export const HOSTILE_TEXT =
  '<img src=x onerror="alert(1)"> structuring ladder · 9_999_999 UGX — ‹near-miss› — {"exposure":412000000} — a very long reason code that has to be truncated without losing the value it names and without ever becoming markup';

export function LongTextRow(): ReactElement {
  return (
    <div style={{ minWidth: 0 }}>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>
        long, markup-shaped text: truncated, titled, escaped
      </p>
      <div style={{ maxWidth: 460, ...PANEL_SUNKEN, padding: 10 }}>
        <p
          data-long-text
          title={HOSTILE_TEXT}
          style={{ ...T_LABEL, color: 'var(--color-ink)', margin: 0, ...ELLIPSIS }}
        >
          {HOSTILE_TEXT}
        </p>
      </div>
    </div>
  );
}

/** Two requests, one key, out of order. The guard decides what is painted. */
export function StaleRow(): ReactElement {
  const [result, setResult] = useState<{ accepted: string; discarded: number } | null>(null);

  useEffect(() => {
    let discarded = 0;
    const key = 'gallery-stale-demo';
    const first = begin(key);
    const second = begin(key);
    // The second request resolves first; the first resolves after it and must lose.
    const timers = [
      setTimeout(() => {
        if (isStale(key, second)) discarded += 1;
        else setResult({ accepted: 'second', discarded });
      }, 20),
      setTimeout(() => {
        if (isStale(key, first)) discarded += 1;
        else setResult({ accepted: 'first', discarded });
      }, 200),
    ];
    return () => timers.forEach((timer) => clearTimeout(timer));
  }, []);

  return (
    <div style={{ ...PANEL_SUNKEN, padding: 10 }}>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '0 0 4px' }}>
        stale-response guard: two requests, the older one answering last
      </p>
      <p data-stale-demo style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: 0 }}>
        {result === null
          ? 'running…'
          : `painted: ${result.accepted} · discarded: ${String(result.discarded)} superseded response${result.discarded === 1 ? '' : 's'}`}
      </p>
    </div>
  );
}
