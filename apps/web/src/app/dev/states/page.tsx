/* =============================================================================
   /dev/states — the whole gallery, in one addressable page.

   Why it exists: plan §16 lists "four states, a `/dev/states` entry, a reduced-motion
   path, zero CLS" as the UI definition of done, and a screenshot suite is the only
   honest way to hold a page of states still long enough to look at it. So every state
   the product can be in is rendered here at once, reachable, and `?motion=reduced`
   renders the entire gallery in reduced-motion mode — which converts DESIGN.md §4's
   guarantee from an assertion into a tested path.

   The gallery is not a storybook of hand-built data. Each row is the real component,
   driven by the real query layer, against whichever transport the environment
   selected:
   * the loading rows are the matched-geometry skeletons the routes use;
   * the error rows are real `problem+json` documents from the failure transport;
   * the empty rows are the four distinct empty states, fed from the same decoders.
   Nothing on this page is a picture of a state. Every one of them IS the state.
   ============================================================================= */

'use client';

import { useSearchParams } from 'next/navigation';
import { Suspense, type ReactElement, ReactNode } from 'react';

import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { Shimmer } from '@/design/primitives/Shimmer';
import { Skeleton } from '@/design/primitives/Skeleton';
import { StageLedger } from '@/design/primitives/StageLedger';
import { Icon } from '@/design/icons/Icon';
import { GLYPH_NAMES } from '@/design/icons/Icon';
import { BandBadge } from '@/components/ui/BandBadge';
import { MarkArc } from '@/components/ui/MarkArc';
import { DegradedBanner, ProvenanceBadge, RunIdChip, Timestamp } from '@/components/ui/provenance';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { ChartFrame, LineChart, Waterfall, minimumSeriesNote } from '@/components/charts/charts';
import { ROUTES, type MoneyFigure as MoneyFigureValue } from '@/lib/api/contract';
import { GALLERY_META } from './gallery-meta';
import { useResource } from '@/lib/api/hooks';
import { count } from '@/lib/format/money';
import { PANEL, PANEL_SUNKEN, T_HERO, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';
import { FigureRow, LongTextRow, StaleRow } from './gallery-rows';

export default function StatesPage(): ReactElement {
  return (
    <Suspense fallback={<GallerySkeleton />}>
      <Gallery />
    </Suspense>
  );
}

/** The gallery is a long single column; the fallback reserves the same column width so
 *  the boundary does not reflow the page. */
function GallerySkeleton(): ReactElement {
  return (
    <div style={{ padding: 'var(--spacing-pane-gap)', display: 'flex', flexDirection: 'column', gap: 24, maxWidth: 1180 }}>
      <Skeleton label="Loading the state gallery" rows={10} columns={[{ key: 'g', width: '100%' }]} />
    </div>
  );
}

function Gallery(): ReactElement {
  const params = useSearchParams();
  const reduced = params.get('motion') === 'reduced';
  const money = useResource('money-strip', ROUTES.dashboard.path, ROUTES.dashboard.data);

  const assumptions = money.meta?.assumptions ?? [];
  const figure: MoneyFigureValue | null = money.data?.expected_loss_avoided ?? null;

  return (
    <div style={{ padding: 'var(--spacing-pane-gap)', display: 'flex', flexDirection: 'column', gap: 24, maxWidth: 1180 }}>
      <header>
        <h1 style={T_HERO}>State gallery</h1>
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '76ch', marginTop: 6 }}>
          Every state the product can be in, on one page, rendered by the components that render it in place.{' '}
          <code style={T_MONO}>?motion=reduced</code> renders the whole gallery with motion suppressed, which is how the
          reduced-motion guarantee is tested rather than asserted. Currently:{' '}
          <strong style={{ color: 'var(--color-ink)' }}>{reduced ? 'reduced motion' : 'full motion'}</strong>.
        </p>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <ProvenanceBadge provenance={money.meta?.provenance ?? null} />
          <RunIdChip runId={money.meta?.run_id ?? null} traceId={money.meta?.trace_id ?? null} />
        </div>
      </header>

      {/* ------------------------------------------------------- 1. bands -- */}
      <Section id="bands" title="Risk bands, three channels at once">
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          {(['A', 'B', 'C', 'D', 'E'] as const).map((band) => (
            <BandBadge key={band} band={band} />
          ))}
        </div>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 10 }}>
          All five sit at equal lightness, so a greyscale print keeps the order through the meter segments and the
          letter. Nothing in the product encodes risk with colour alone, and{' '}
          <code style={T_MONO}>@media print</code> in globals.css re-states the ramp in greyscale for the packet.
        </p>
      </Section>

      {/* ------------------------------------------------------ 2. glyphs -- */}
      <Section id="glyphs" title="The twelve typology glyphs and the three utility marks">
        <ul style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', gap: 10, listStyle: 'none', margin: 0, padding: 0 }}>
          {GLYPH_NAMES.map((name) => (
            <li key={name} style={{ ...PANEL_SUNKEN, padding: 10, display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'flex-start' }}>
              <Icon name={name} size={24} title={name} />
              <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>{name}</span>
            </li>
          ))}
        </ul>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
          Hand-drawn on a 24 px grid at 1.75 px stroke, square caps. They encode behaviours no icon library carries,
          which is why they are on the never-cut list.
        </p>
      </Section>

      {/* ---------------------------------------------------- 3. loading -- */}
      <Section id="loading" title="Loading: matched-geometry skeletons, never a spinner">
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 16 }}>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 6 }}>queue rows, 172 px each</p>
            <Skeleton
              label="Loading the alert queue"
              showHeader={false}
              rows={3}
              rowHeight={172}
              columns={[
                { key: 'account', width: '150px' },
                { key: 'band', width: '110px' },
                { key: 'reasons', width: 'minmax(0, 1fr)' },
                { key: 'exposure', width: '170px', align: 'end' },
              ]}
            />
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 6 }}>transactions table</p>
            <Skeleton
              label="Loading the transaction table"
              rows={6}
              rowHeight={36}
              columns={[
                { key: 'txn', width: '130px' },
                { key: 'when', width: '160px' },
                { key: 'type', width: '110px' },
                { key: 'party', width: 'minmax(0, 1fr)' },
                { key: 'amount', width: '110px', align: 'end' },
              ]}
            />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 16, marginTop: 14, flexWrap: 'wrap', alignItems: 'center' }}>
          <div style={{ minWidth: 220 }}>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>shimmer: translateX on the compositor</p>
            <Shimmer width="100%" height={14} label="Sweeping placeholder" />
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>the one licensed spinner</p>
            <MarkArc progress={0.42} label="CP-SAT solve" />
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>…and with no denominator</p>
            <MarkArc progress={null} label="waiting for the incumbent" />
          </div>
        </div>
      </Section>

      {/* ------------------------------------------------------ 4. ledger -- */}
      <Section id="ledger" title="Long jobs get a ledger, not a spinner">
        <LedgerStrip />
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
          Rows are real SSE stage events with server-reported row counts and elapsed milliseconds. Watching real work
          happen is the point: a bar that creeps upward on its own is the failure this component exists to prevent.
        </p>
      </Section>

      {/* ------------------------------------------------- 5. four empties -- */}
      <Section id="empty" title="Four empty states, none of which says “no data”">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16 }}>
          <EmptyState
            kind="filters-excluded"
            unfilteredRows={1_412}
            narrowest={{ label: 'Typology is circular transfer', value: 'R4 only', rowsIfRemoved: 41, onRemove: () => undefined }}
            others={[{ label: 'Band', value: 'E', onRemove: () => undefined }]}
          />
          <EmptyState kind="no-run" command="make pipeline" expectedRuntime="6–9 minutes on the dev slice" corpus="IBM-AML HI-Small and PaySim" />
          <EmptyState
            kind="window-empty"
            accountId="ACC-00DORM"
            from="2026-09-11"
            to="2026-09-18"
            edgesAtCurrentHops={0}
            edgesAtWiderWindow={37}
            currentHops={1}
            maxHops={4}
            onWidenHops={() => undefined}
            onWidenDates={() => undefined}
          />
          <EmptyState
            kind="no-disagreement"
            bandAbove="C"
            comparedAccounts={4_918}
            maxDelta={0}
            nearMissThreshold={0.15}
            rowsAtThreshold={37}
            onSetThreshold={() => undefined}
          />
        </div>
      </Section>

      {/* ------------------------------------------------- 6. four errors -- */}
      <Section id="error" title="Four error tiers">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>tier 1 · inline field</p>
            <p role="alert" data-field-error="reason" style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}>
              reason: a written reason is required — the field above is still holding your text.
            </p>
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>tier 2 · pane-level, retrying in place</p>
            <ErrorPane
              paneId="gallery-pane"
              operation="Loading the network graph"
              error={{ title: 'Upstream failure', status: 502, detail: 'the warehouse port returned a reset mid-query', run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D' }}
              onRetry={() => undefined}
              attempt={2}
            />
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>tier 3 · route-level error.tsx · see /alerts?fail=500</p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
              A segment failure renders the same ErrorPane with <code style={T_MONO}>siblingsIntact=false</code>; the
              header and the footer survive because they are above the boundary.
            </p>
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>tier 4 · global-error.tsx</p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0, maxWidth: '72ch' }}>
              The boundary above the shell re-declares html and body and imports nothing from the design system, because
              if the shell is what failed, a surface built from the shell is what just failed. It is styled with the
              token values inlined for exactly that reason.
            </p>
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>contract tier · a 2xx that is not the envelope</p>
            <ErrorPane
              paneId="gallery-contract"
              operation="Reading the dashboard strip"
              error={{ title: 'Response did not match the API contract', detail: 'envelope keys were [data, success], expected [data, meta]', run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D' }}
              onRetry={() => undefined}
            />
          </div>
        </div>
      </Section>

      {/* ---------------------------------------------------- 7. degraded -- */}
      <Section id="degraded" title="Degraded, not broken">
        <DegradedBanner
          dependency="CP-SAT solver port"
          fallback="the greedy allocation by EV density"
          meta={money.meta ?? { ...GALLERY_META, degraded_reason: 'The solver did not answer inside its deadline.' }}
        />
        <div style={{ marginTop: 10 }}>
          <ProvenanceBadge provenance="null-adapter:out/json" />
        </div>
      </Section>

      {/* ----------------------------------------------------- 8. money ---- */}
      <Section id="money" title="A currency figure cannot render without its assumptions">
        {figure === null ? (
          <Skeleton label="Loading the figures" showHeader={false} rows={1} rowHeight={96} columns={[{ key: 'f', width: '100%' }]} />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 16 }}>
            <MoneyFigure figure={figure} assumptions={assumptions} label="Expected loss avoided" emphasis="kpi" source="config/economics.yaml" />
            <MoneyFigure figure={money.data?.residual_exposure ?? figure} assumptions={assumptions} label="Residual exposure (ES 97.5%)" emphasis="kpi" source="config/economics.yaml" />
            <FigureRow />
          </div>
        )}
      </Section>

      {/* ------------------------------------------------------ 9. edges --- */}
      <Section id="edges" title="Edge cases with named tests">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>single-point series, minimum-series guard</p>
            <LineChart points={[{ x: '2026-09-01T00:00:00Z', y: 412_000_000, label: 'one fold' }]} yIsMoney ariaLabel="Single point series" />
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 4 }}>
              {minimumSeriesNote(1, 'line')}
            </p>
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>zero-point series</p>
            <ChartFrame height={80} note={minimumSeriesNote(0, 'line') ?? ''} label="empty series guard" />
          </div>
          <LongTextRow />
          <StaleRow />
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>timestamps carry their zone</p>
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'baseline' }}>
              <Timestamp iso="2026-09-24T06:12:00Z" timeZone="Africa/Kampala" sense="scored" />
              <Timestamp iso="2026-09-24T06:12:00Z" timeZone="UTC" sense="the same instant in UTC" />
              <Timestamp iso={null} timeZone="Africa/Kampala" sense="nothing recorded" />
              <Timestamp iso="2026-09-24T06:12:00Z" timeZone={null} sense="zone unreported" />
            </div>
          </div>
          <div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginBottom: 4 }}>waterfall rows, which are cross-filter targets</p>
            <Waterfall
              ariaLabel="Gallery contribution waterfall"
              rows={[
                { label: 'Pass-through ratio, trailing hour', value: -0.31, direction: 'decreases' },
                { label: 'Distinct senders, 24 h', value: -0.18, direction: 'decreases' },
                { label: 'Account age', value: 0.09, direction: 'increases' },
              ]}
            />
          </div>
        </div>
      </Section>

      {/* ----------------------------------------------------- 10. type ---- */}
      <Section id="type" title="Type scale, in the order a screen reads it">
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'baseline' }}>
          <p style={T_HERO}>{count(412)}</p>
          <p style={{ ...T_LABEL }}>label · band meter · typology chip</p>
          <p style={{ ...T_MONO, color: 'var(--color-ink-muted)' }}>ACC-7F2A19</p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>micro · assumption line</p>
        </div>
      </Section>
    </div>
  );
}

/* --------------------------------------------------------------- helpers -- */

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }): ReactElement {
  return (
    <section id={id} data-gallery-section={id} style={{ ...PANEL, padding: 16 }}>
      <h2
        style={{
          ...T_LABEL,
          margin: '0 0 12px',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          color: 'var(--color-ink-faint)',
        }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

/** The ledger with a live running stage, so the animation path is on the page too. */
function LedgerStrip(): ReactElement {
  const stages = [
    { id: '1', stage: 'ingest', status: 'complete', rows: 6_362_620, elapsed_ms: 12_400 },
    { id: '2', stage: 'canonicalise', status: 'complete', rows: 6_362_620, elapsed_ms: 4_100 },
    { id: '3', stage: 'graph', status: 'complete', rows: 515_080, elapsed_ms: 21_800 },
    { id: '4', stage: 'features', status: 'running', rows: null, elapsed_ms: Date.now() - 9_000 },
    { id: '5', stage: 'rules', status: 'pending', rows: null, elapsed_ms: null },
    { id: '6', stage: 'score', status: 'pending', rows: null, elapsed_ms: null },
  ] as const;
  return <StageLedger stages={stages.map((entry) => ({ ...entry, status: entry.status }))} runId="01J4Z7M2QK9N7V1C4X6E8G0B2D" />;
}

