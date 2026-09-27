/* =============================================================================
   Tier 4: `global-error.tsx`. The boundary above the root layout, so it is the only
   error surface that can render when the shell itself is what failed — a font that
   did not load, a provider that threw, a theme attribute nobody set.

   It re-declares <html> and <body> because that is what Next requires of this file,
   and it deliberately imports nothing from the design system: if the failure is in
   the shell, a surface built from the shell is the thing that has just failed. The
   values are the token literals inlined as text, which is the one place in the app
   where repeating a colour is the correct engineering choice.
   ============================================================================= */

'use client';

import type { ReactElement } from 'react';

export default function GlobalError({
  error,
  reset,
}: { error: Error & { digest?: string }; reset: () => void }): ReactElement {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          padding: 24,
          background: 'oklch(0.17 0.012 250)',
          color: 'oklch(0.96 0.006 250)',
          fontFamily: '"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif',
          fontSize: '0.875rem',
          lineHeight: 1.5,
        }}
      >
        <h1 style={{ fontSize: '1.125rem', fontWeight: 600, margin: '0 0 8px' }}>OXBOW could not start</h1>
        <p style={{ margin: '0 0 8px', maxWidth: '68ch', color: 'oklch(0.74 0.01 250)' }}>
          The failure is above the application shell, so nothing else on this screen can be trusted to render. The
          identifier below is what the server log and this conversation share.
        </p>
        <p style={{ margin: '0 0 8px' }}>
          <code
            style={{
              fontFamily: '"IBM Plex Mono", ui-monospace, Menlo, monospace',
              fontSize: '0.75rem',
              padding: '2px 6px',
              border: '1px solid oklch(0.86 0.01 250 / 0.16)',
              borderRadius: 3,
              userSelect: 'all',
            }}
          >
            {error.digest ?? error.name}
          </code>
        </p>
        <button
          type="button"
          onClick={reset}
          style={{
            fontFamily: 'inherit',
            fontSize: '0.75rem',
            fontWeight: 500,
            color: 'oklch(0.17 0.012 250)',
            background: 'oklch(0.96 0.006 250)',
            border: '1px solid oklch(0.96 0.006 250)',
            borderRadius: 3,
            padding: '4px 10px',
            cursor: 'pointer',
          }}
        >
          Try again
        </button>
        <p style={{ marginTop: 24, fontSize: '0.6875rem', color: 'oklch(0.56 0.012 250)', maxWidth: '84ch' }}>
          OXBOW is a research prototype that analyzes historical, de-identified data only. It does not process live
          financial transactions, does not trade or advise on any financial instrument, does not make real financial
          decisions, and is not financial advice. Monetary figures are model estimates derived from stated assumptions,
          not measured outcomes. Results are not validated for operational use by any financial institution.
        </p>
      </body>
    </html>
  );
}
