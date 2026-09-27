/* =============================================================================
   The shell: header with the dataset and licence badge, navigation, and the
   disclaimer in the footer of every page.

   Two of those are contract clauses rather than layout choices. DESIGN.md §8 and
   plan §15 require the disclaimer on every page and assert it in a test, so it is
   rendered here once, outside every route, where a route cannot forget it. Plan §14
   requires the dataset and licence badge in the header, and the badge reads from
   `GET /api/meta/run`: a licence string typed into JSX would be a claim about a
   dataset this layer has never inspected.

   The provenance banner lives here too, for the same reason — one place, so no
   screen can present fixture bytes without saying so.
   ============================================================================= */

'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { type ReactElement, type ReactNode, Suspense, useEffect } from 'react';

import { Icon } from '../design/icons/Icon';
import { useRuntime } from '../lib/api/hooks';
import { transportMode } from '../lib/api/transport';
import { DISCLAIMER, SCENARIO_NOTE } from '../lib/copy';
import { useShell } from './AppProviders';
import { ProvenanceBadge } from './ui/provenance';
import { GAP, T_LABEL, T_MICRO } from './ui/sx';

const NAV: readonly { href: string; label: string; hint: string; w: number }[] = [
  // `w` is the label's width in the loaded IBM Plex Sans at the desktop breakpoint.
  // Nav links carry it as a minimum so the font swap cannot re-pack the strip: a
  // fallback-rendered label is up to 3 px narrower across the six items, and that
  // ±3px is the layout shift the zero-CLS gate counts.
  { href: '/dashboard', label: 'Command', hint: 'currency strip and the shape of the period', w: 65 },
  { href: '/alerts', label: 'Queue', hint: 'ranked under the active policy, with the capacity line', w: 47 },
  { href: '/network', label: 'Network', hint: 'two hops, overlays, time scrubber', w: 57 },
  { href: '/scorecard', label: 'Scorecard', hint: 'points, bands, drift, disagreement', w: 65 },
  { href: '/policy', label: 'Policy', hint: 're-allocation under capacity and recovery rate', w: 45 },
  { href: '/model', label: 'Validation', hint: 'walk-forward, ablation, limitations', w: 64 },
];

export function Shell({ children }: { children: ReactNode }): ReactElement {
  const pathname = usePathname();
  const runtime = useRuntime();
  const shell = useShell();
  const meta = runtime.meta;

  const provenance = runtime.meta?.provenance ?? transportProvenanceAtFirstPaint();
  /* The banner is decided by the transport the build selected, which is known on the
     first render, and not by a response field that arrives ~4s later. Deriving it from
     `meta.provenance` alone made the banner appear after every pane had already painted
     and pushed the whole page down — the single largest layout shift measured on this
     app (0.39 on the queue, 0.34 on the explorer). The response still gets the last
     word: `isFixture(provenance)` keeps the banner up if a payload ever arrives stamped
     as fixture bytes over the real transport. */
  const fixtureTransport = transportMode() !== 'api';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh', background: 'var(--color-canvas)' }}>
      {/* Reading the query string is a client-only operation, so it is isolated here
          behind its own boundary rather than in the shell body: the header, the
          disclaimer footer and the fixture banner stay server-rendered on every route,
          and only this null-rendering effect opts out of static rendering. */}
      <Suspense fallback={null}>
        <MotionQuery />
      </Suspense>
      <header
        data-print-hide
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 20,
          borderBottom: '1px solid var(--color-hairline)',
          background: 'var(--color-canvas-raised)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            padding: '8px var(--spacing-pane-gap)',
            flexWrap: 'wrap',
          }}
        >
          <Link href="/dashboard" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {/* The mark, at its natural size. This is the only place the glyph is a
                brand rather than a control. */}
            <Image />
            <span
              style={{
                fontFamily: 'var(--font-condensed)',
                fontSize: '1.125rem',
                fontWeight: 600,
                letterSpacing: '0.08em',
                color: 'var(--color-ink)',
                /* 64 px is the wordmark's width in the fallback face at the desktop
                   breakpoint; IBM Plex Sans Condensed sets 6 px narrower. Left-aligned in
                   a box pinned to the wider of the two, so the swap cannot slide the nav —
                   the same swap-stable trick the nav items and the footer use. */
                minWidth: 64,
              }}
            >
              OXBOW
            </span>
          </Link>

          <nav aria-label="Sections" style={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
            {NAV.map((item) => {
              const active = pathname?.startsWith(item.href) ?? false;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.hint}
                  aria-current={active ? 'page' : undefined}
                  style={{
                    ...T_LABEL,
                    padding: '4px 8px',
                    borderRadius: 'var(--radius-control)',
                    minWidth: item.w,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: active ? 'var(--color-ink)' : 'var(--color-ink-muted)',
                    background: active ? 'var(--color-elev-2)' : 'transparent',
                    borderBottom: active ? '2px solid var(--color-evidence)' : '2px solid transparent',
                  }}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {/* The dataset chip is mounted from the first paint at a fixed left edge, in a
                box the width of the widest string it can hold. A chip that simply appears
                when `GET /api/meta/run` answers takes up to 360 px out of the header and
                moves every element that was already on screen — measured at 0.0052 on the
                queue once the transport is same-origin, which is five times the CLS budget
                on a route that has nothing to do with the header. Reserving the box rather
                than the ink is §5's matched-geometry rule applied to the shell: the chip
                says "no dataset reported" until the response says otherwise, and its own
                left edge never moves. */}
            <span style={{ width: 360, display: 'inline-flex', alignItems: 'center', flexShrink: 0 }} data-dataset-slot>
              <span
                data-dataset-badge
                title={`${runtime.data?.dataset ?? 'no dataset reported'} · ${runtime.data?.licence ?? 'no licence reported'}`}
                style={{
                  ...T_MICRO,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '2px 8px',
                  border: '1px solid var(--color-hairline-strong)',
                  borderRadius: 'var(--radius-control)',
                  color: 'var(--color-ink-muted)',
                  maxWidth: 360,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                <Icon name="chain" size={12} />
                {runtime.data?.dataset ?? 'no dataset'} · {runtime.data?.licence ?? 'licence unreported'}
              </span>
            </span>

            <ProvenanceBadge provenance={provenance} />

            <button
              type="button"
              onClick={() => shell.setTheme(shell.theme === 'canvas' ? 'paper' : 'canvas')}
              style={{
                ...T_LABEL,
                padding: '2px 8px',
                border: '1px solid var(--color-hairline-strong)',
                borderRadius: 'var(--radius-control)',
                background: 'transparent',
                color: 'var(--color-ink-muted)',
                cursor: 'pointer',
              }}
            >
              {shell.theme === 'canvas' ? 'Paper' : 'Canvas'}
            </button>
          </div>
        </div>
      </header>

      {fixtureTransport || isFixture(provenance) ? (
        <div
          role="status"
          data-fixture-banner
          style={{
            ...T_MICRO,
            padding: '6px var(--spacing-pane-gap)',
            background: 'var(--color-canvas-sunken)',
            borderBottom: '1px solid var(--color-hairline)',
            color: 'var(--color-state-failed)',
          }}
        >
          These screens are rendering the developer contract fixtures because the API is not answering. Every figure
          below is sample data shaped by the client contract, not a pipeline result, and the transport that serves it is
          unreachable in a production build.
        </div>
      ) : null}

      <main style={{ flex: 1, minWidth: 0 }}>{children}</main>

      <footer
        data-print-block
        style={{
          borderTop: '1px solid var(--color-hairline)',
          background: 'var(--color-canvas-sunken)',
          padding: '12px var(--spacing-pane-gap)',
        }}
      >
        <p data-disclaimer style={{ ...T_MICRO, color: 'var(--color-ink-muted)', maxWidth: '84ch', ...GAP }}>
          {meta?.disclaimer ?? DISCLAIMER}
        </p>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 6 }}>{SCENARIO_NOTE}</p>
        <div style={{ display: 'flex', gap: 12, marginTop: 8, flexWrap: 'wrap' }}>
          <Link href="/dev/states" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            state gallery
          </Link>
          <Link href="/dev/states?motion=reduced" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            reduced-motion gallery
          </Link>
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            every currency figure on this site is a function of the assumptions printed beside it
          </span>
        </div>
      </footer>
    </div>
  );
}

function isFixture(provenance: string | null): boolean {
  return provenance?.startsWith('fixture') ?? false;
}

/**
 * The provenance the *transport* already knows, before any response arrives.
 *
 * The fixture double stamps every payload it serves `fixture:developer-contract`, and
 * the transport mode is a build-time fact rather than a network fact, so the header can
 * state it on the first paint. Waiting for the lazily-imported fixture module to answer
 * ~3 s later grew the provenance badge from 170 px to 401 px and slid the whole right
 * rail with it — 0.0019 of CLS on a route that has nothing to do with the header. The
 * real transport knows nothing yet and says so, which is what `null` renders as.
 */
function transportProvenanceAtFirstPaint(): string | null {
  return transportMode() === 'fixture' ? 'fixture:developer-contract' : null;
}

/**
 * The gallery's forced-reduced-motion path: `?motion=reduced` sets the attribute
 * globals.css keys off and flips the MotionConfig, so the guarantee is a route you can
 * load and screenshot rather than a claim in a comment. It renders nothing, which is why
 * isolating it costs the page no geometry and no shift.
 */
function MotionQuery(): ReactElement | null {
  const params = useSearchParams();
  const shell = useShell();

  useEffect(() => {
    const forced = params.get('motion') === 'reduced';
    if (typeof document !== 'undefined') {
      document.documentElement.dataset.motion = forced ? 'reduced' : 'full';
    }
    shell.setReducedMotion(forced);
  }, [params, shell]);

  return null;
}

/** The wordless brand mark: the same sprite the rest of the product uses. */
function Image(): ReactElement {
  return (
    <svg width={22} height={22} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <use href="/mark.svg#mark" />
    </svg>
  );
}
