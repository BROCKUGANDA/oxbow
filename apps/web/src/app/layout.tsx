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
