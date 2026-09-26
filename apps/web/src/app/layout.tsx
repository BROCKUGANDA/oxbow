/* =============================================================================
   Root layout: the shell every route shares.

   Three obligations, all design-contract clauses rather than taste:
   * fonts. IBM Plex Sans / Mono / Condensed, self-hosted through `next/font` so the
     product never adds a third-party request. DESIGN.md §3 bans Inter and Geist,
     and a system-font fallback stack would ship the default look the gate exists to
     prevent — these are the real faces, loaded from the pinned dependency.
   * theme. Dark canvas is the default; paper mode is a `data-theme` attribute on the
     html element, which is what lets the token file re-point names rather than
     making a component branch.
   * motion. `MotionConfig reducedMotion="user"` globally, plus the
     `?motion=reduced` override the gallery is tested under.
   ============================================================================= */

import type { Metadata, Viewport } from 'next';
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from 'next/font/google';
import type { ReactElement, ReactNode } from 'react';

import './globals.css';
import { AppProviders } from '@/components/AppProviders';
import { Shell } from '@/components/Shell';

const sans = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-loaded-sans',
  display: 'swap',
});

const mono = IBM_Plex_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-loaded-mono',
  display: 'swap',
});

const condensed = IBM_Plex_Sans_Condensed({
  subsets: ['latin'],
  weight: ['500', '600'],
  variable: '--font-loaded-condensed',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'OXBOW — transaction-monitoring research prototype',
  description:
    'Scores financial-crime risk on historical de-identified mobile-money data, explains every score, and prices every alert in money and analyst-hours.',
};

export const viewport: Viewport = {
  themeColor: '#0d1420',
};

export default function RootLayout({ children }: { children: ReactNode }): ReactElement {
  /* Tier 4 of the error ladder is `global-error.tsx`, and Next mounts it for exactly one
   * class of failure: one thrown above the root error boundary, which in this app means
   * thrown *here*, in the layout every route renders through. Without a trigger the
   * fourth tier is a component nobody has ever seen painted, so the state gallery's
   * claim that the ladder is complete would be an assertion.
   *
   * `OXBOW_GLOBAL_ERROR_PROBE=1` is that trigger — a server-side environment variable,
   * unreadable from a URL and never set outside a deliberate run of the browser checks.
   * It is checked before anything renders, because the point is to fail the shell. */
  if (process.env.OXBOW_GLOBAL_ERROR_PROBE === '1') {
    throw new Error('global-error probe: the root layout was asked to fail (OXBOW_GLOBAL_ERROR_PROBE=1)');
  }

  return (
    <html
      lang="en"
      data-theme="canvas"
      className={`${sans.variable} ${mono.variable} ${condensed.variable}`}
      suppressHydrationWarning
    >
      <body>
        <AppProviders>
          <Shell>{children}</Shell>
        </AppProviders>
      </body>
    </html>
  );
}
