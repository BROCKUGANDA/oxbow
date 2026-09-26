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
import { Suspense, useEffect, type ReactElement, type ReactNode } from 'react';

import { Icon } from '../design/icons/Icon';
import { ProvenanceBadge } from './ui/provenance';
import { useRuntime } from '../lib/api/hooks';
import { DISCLAIMER, SCENARIO_NOTE } from '../lib/copy';
import { GAP, T_LABEL, T_MICRO } from './ui/sx';
import { useShell } from './AppProviders';

const NAV: readonly { href: string; label: string; hint: string }[] = [
  { href: '/dashboard', label: 'Command', hint: 'currency strip and the shape of the period' },
  { href: '/alerts', label: 'Queue', hint: 'ranked under the active policy, with the capacity line' },
  { href: '/network', label: 'Network', hint: 'two hops, overlays, time scrubber' },
  { href: '/scorecard', label: 'Scorecard', hint: 'points, bands, drift, disagreement' },
  { href: '/policy', label: 'Policy', hint: 're-allocation under capacity and recovery rate' },
  { href: '/model', label: 'Validation', hint: 'walk-forward, ablation, limitations' },
];

export function Shell({ children }: { children: ReactNode }): ReactElement {
  const pathname = usePathname();
  const runtime = useRuntime();
  const shell = useShell();
  const meta = runtime.meta;

  const provenance = meta?.provenance ?? null;

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
            {runtime.data !== null ? (
              <span
                data-dataset-badge
                title={`${runtime.data.dataset ?? 'no dataset reported'} · ${runtime.data.licence ?? 'no licence reported'}`}
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
                {runtime.data.dataset ?? 'no dataset'} · {runtime.data.licence ?? 'licence unreported'}
              </span>
            ) : null}

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

      {isFixture(provenance) ? (
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
          These screens are rendering the developer contract fixtures because the API is not answering. Every
          figure below is sample data shaped by the client contract, not a pipeline result, and the transport that
          serves it is unreachable in a production build.
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
  return provenance !== null && provenance.startsWith('fixture');
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
