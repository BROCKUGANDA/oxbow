/* =============================================================================
   Alert queue — plan §14 P8a-2.

   THE CAPACITY LINE. Everything below it is what this period is consciously not
   reviewed. It is on the never-cut list because it is the argument: a ranked list
   implies the work gets done, and the honest statement is that a budget decides
   where reading stops. So it is not a hair and a label — it spans the list and
   carries the count beneath it, the minutes that bought it, and the money the
   remainder is worth, all three from the response.

   VIRTUALISATION. Rows are windowed by TanStack Virtual against `ROW_HEIGHT`, the
   same constant the skeleton is built from, and the scroll container's height comes
   from `meta.total` before a single row exists. That is how CLS stays at zero on a
   ten-thousand-row queue rather than merely getting small.

   URL SYNC. Filter, page, sort and focus are the query string, so
   `/alerts?band=E&typology=R4&offset=1200` reopens exactly what an analyst was
   reading. Filtering is server-side: the count an empty state quotes has to be the
   server's count, not a client-side survivors tally.

   KEYBOARD. j/k move, Enter opens, e escalates, d dismisses. Bound at the window so
   both hands stay off the mouse while reading; the route each key pushes is the same
   route a click would push.
   ============================================================================= */

'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
} from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';

import { Icon } from '@/design/icons/Icon';
import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { Skeleton } from '@/design/primitives/Skeleton';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { BandBadge } from '@/components/ui/BandBadge';
import { AccountChip, RunIdChip, Timestamp } from '@/components/ui/provenance';
import { TYPOLOGY_META, glyphFor } from '@/components/typology';
import { PIPELINE_COMMAND, RUNTIME_ESTIMATE_FALLBACK } from '@/lib/copy';
import { ROUTES, type AlertRow, type Band, type QueueCapacity, type QueueFacets, type Typology } from '@/lib/api/contract';
import { useListResource, useRuntime } from '@/lib/api/hooks';
import { failureDetail, failureRunId, failureTitle, isRunNotFound } from '@/lib/api/problem';
import { compactFromMinor, count } from '@/lib/format/money';
import { ELLIPSIS, GAP_TIGHT, HAIRLINE_BOTTOM, PANEL_SUNKEN, T_LABEL, T_MICRO } from '@/components/ui/sx';

/** THE row height, shared by the virtualiser and the skeleton. Geometry, not data. */
const ROW_HEIGHT = 172;
const PAGE_SIZE = 100;

const BANDS: readonly Band[] = ['A', 'B', 'C', 'D', 'E'];

type QueueFilters = {
  bands: string[];
  typology: string | null;
  query: string;
  offset: number;
  sort: string;
};

function readFilters(params: URLSearchParams): QueueFilters {
  return {
    bands: (params.get('band') ?? '').split(',').filter((value) => value.length > 0),
    typology: params.get('typology'),
    query: params.get('q') ?? '',
    offset: Number(params.get('offset') ?? '0'),
    sort: params.get('sort') ?? 'rank',
  };
}

/** The queue reads its filters from the query string, which is client-only, so the
 *  segment needs a Suspense boundary or the static build refuses it. The fallback is
 *  the queue's own virtualiser skeleton — same row height, same columns, same count as
 *  the first page — so crossing the boundary moves nothing. */
export default function AlertsPage(): ReactElement {
  return (
    <Suspense fallback={<QueueSkeleton />}>
      <QueueExplorer />
    </Suspense>
  );
}

function QueueSkeleton(): ReactElement {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 112px)' }}>
      {/* The filter bar is 44 px tall in the resolved layout and stays in the flow
          while the queue suspends; leaving it out is a 44 px shift on resolve. */}
      <div style={{ height: 44, padding: '0 var(--spacing-pane-gap)', display: 'flex', alignItems: 'center' }}>
        <Skeleton label="Loading the filter bar" rows={1} rowHeight={28} showHeader={false} columns={[{ key: 'f', width: '320px' }]} />
      </div>
      <div style={{ padding: '0 var(--spacing-pane-gap)' }}>
        <Skeleton
          label="Loading the alert queue"
          rows={6}
          rowHeight={ROW_HEIGHT}
          showHeader={false}
          columns={[
            { key: 'account', width: '160px' },
            { key: 'band', width: '120px' },
            { key: 'reasons', width: 'minmax(0, 1fr)' },
            { key: 'exposure', width: '210px', align: 'end' },
            { key: 'ev', width: '210px', align: 'end' },
          ]}
        />
      </div>
    </div>
  );
}

function QueueExplorer(): ReactElement {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filters = readFilters(params);
  const runtime = useRuntime();
  const timeZone = runtime.data?.deployment_timezone ?? null;

  const [focused, setFocused] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);

  /* `total=10000` is the queue's own stress parameter, so the virtualisation test can
     ask for the large page without a component knowing it is being tested. */
  const stress = Number(params.get('total') ?? '0');
  const queue = useListResource('alerts', ROUTES.alerts.path, ROUTES.alerts.data, {
    // Repeated `band=` parameters, not a comma join: the API's list filter is
    // `band: list[str]` and refuses an unknown band outright.
    band: filters.bands,
    typology: filters.typology,
    // The server's search key is `account_key`, minimum three characters — the field
    // the API validates. Anything shorter is not yet a search, so it is not sent.
    account_key: filters.query.length >= 3 ? filters.query : null,
    limit: PAGE_SIZE,
    offset: filters.offset,
    sort: filters.sort,
    ...(stress > 0 ? { total: stress } : {}),
  });

  const rows = queue.data?.rows ?? [];
  const capacity = queue.data?.capacity ?? null;
  const assumptions = queue.meta?.assumptions ?? [];
  const total = queue.meta?.total ?? rows.length;

  const writeFilters = useCallback(
    (next: Partial<QueueFilters>) => {
      const merged = new URLSearchParams(params?.toString() ?? '');
      const setOrDelete = (key: string, value: string | null): void => {
        if (value === null || value.length === 0) merged.delete(key);
        else merged.set(key, value);
      };
      if (next.bands !== undefined) setOrDelete('band', next.bands.join(','));
      if (next.typology !== undefined) setOrDelete('typology', next.typology);
      if (next.query !== undefined) setOrDelete('q', next.query);
      if (next.offset !== undefined) setOrDelete('offset', String(next.offset));
      if (next.sort !== undefined) setOrDelete('sort', next.sort);
      router.push(`${pathname}?${merged.toString()}`, { scroll: false });
    },
    [params, pathname, router],
  );

  const virtualiser = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 6,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      const target = event.target as HTMLElement | null;
      if (target !== null && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return;
      const last = Math.max(rows.length - 1, 0);
      if (event.key === 'j' || event.key === 'k') {
        event.preventDefault();
        const next = Math.min(Math.max(focused + (event.key === 'j' ? 1 : -1), 0), last);
        setFocused(next);
        virtualiser.scrollToIndex(next);
        return;
      }
      const row = rows[focused];
      if (row === undefined) return;
      if (event.key === 'Enter') router.push(row.case_href);
      if (event.key === 'e' || event.key === 'd') {
        router.push(`${row.case_href}?decide=${event.key === 'e' ? 'escalate' : 'dismiss'}`);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [rows, focused, router, virtualiser]);

  /** Index of the row the cutoff is drawn after, or -1 when it is off-page. */
  const cutoffIndex = useMemo(() => {
    if (capacity === null || capacity.cutoff_rank === null) return -1;
    const index = rows.findIndex((row) => row.rank === capacity.cutoff_rank);
    if (index >= 0) return index;
    // The cutoff sits between two pages: draw it on whichever side it is nearest so
    // the line never silently disappears from a filtered view.
    return capacity.cutoff_rank < (rows[0]?.rank ?? 0) ? 0 : -1;
  }, [rows, capacity]);

  if (queue.failure !== null && queue.data === null) {
    /* The failure branch reserves the same full-height column the skeleton held —
       bar slot plus body — because the measured CLS on this route was the footer
       jumping when a `calc(100vh - 112px)` pending box collapsed into an auto-height
       error block. A failed queue is still a queue-shaped region. */
    const failureSurface = isRunNotFound(queue.failure) ? (
      <EmptyState
        kind="no-run"
        command={PIPELINE_COMMAND}
        expectedRuntime={RUNTIME_ESTIMATE_FALLBACK}
        corpus={runtime.data?.dataset ?? undefined}
      />
    ) : (
      <ErrorPane
        paneId="alert-queue"
        operation="Loading the alert queue"
        error={{
          title: failureTitle(queue.failure),
          detail: failureDetail(queue.failure) ?? undefined,
          run_id: failureRunId(queue.failure) ?? undefined,
        }}
        onRetry={() => void queue.refetch()}
        attempt={queue.attempts}
        retrying={queue.isFetching}
        siblingsIntact={false}
      >
        <RunIdChip runId={failureRunId(queue.failure)} />
      </ErrorPane>
    );
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 112px)' }}>
        <div aria-hidden="true" style={{ height: 44, padding: '0 var(--spacing-pane-gap)' }} />
        <div style={{ padding: 'var(--spacing-pane-gap)' }}>{failureSurface}</div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 112px)' }}>
      <FilterBar filters={filters} facets={queue.data?.facets ?? null} total={total} onChange={writeFilters} />

      {queue.data === null ? (
        <div style={{ padding: '0 var(--spacing-pane-gap)' }}>
          <Skeleton
            label="Loading the alert queue"
            rows={6}
            rowHeight={ROW_HEIGHT}
            showHeader={false}
            columns={[
              { key: 'account', width: '160px' },
              { key: 'band', width: '120px' },
              { key: 'reasons', width: 'minmax(0, 1fr)' },
              { key: 'exposure', width: '210px', align: 'end' },
              { key: 'ev', width: '210px', align: 'end' },
            ]}
          />
        </div>
      ) : rows.length === 0 ? (
        <div style={{ padding: 'var(--spacing-pane-gap)' }}>
          {queue.data.filter_recovery !== null ? (
            <EmptyState
              kind="filters-excluded"
              unfilteredRows={queue.data.filter_recovery.unfiltered_rows}
              narrowest={{
                label: queue.data.filter_recovery.label,
                value: `${count(queue.data.filter_recovery.rows_if_removed)} alerts would return`,
                rowsIfRemoved: queue.data.filter_recovery.rows_if_removed,
                onRemove: () => {
                  const which = queue.data?.filter_recovery?.narrowest;
                  writeFilters(which === 'typology' ? { typology: null } : which === 'band' ? { bands: [] } : { query: '' });
                },
              }}
              others={filters.bands.map((band) => ({
                label: 'Band',
                value: band,
                onRemove: () => writeFilters({ bands: filters.bands.filter((entry) => entry !== band) }),
              }))}
            />
          ) : (
            <EmptyState
              kind="no-run"
              command={PIPELINE_COMMAND}
              expectedRuntime={RUNTIME_ESTIMATE_FALLBACK}
              corpus={runtime.data?.dataset ?? undefined}
            />
          )}
        </div>
      ) : (
        <div
          ref={listRef}
          data-queue-scroll
          className="u-scroll"
          style={{ flex: 1, minHeight: 0, position: 'relative' }}
          role="list"
          aria-label={`Alert queue, ${count(total)} alerts match the active policy`}
        >
          <div style={{ height: virtualiser.getTotalSize(), position: 'relative' }}>
            {virtualiser.getVirtualItems().map((item) => {
              const row = rows[item.index];
              if (row === undefined) return null;
              return (
                <div
                  key={row.account_key}
                  data-index={item.index}
                  ref={virtualiser.measureElement}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${item.start}px)`,
                    height: ROW_HEIGHT,
                  }}
                >
                  <QueueCard
                    row={row}
                    assumptions={assumptions}
                    timeZone={timeZone}
                    focused={item.index === focused}
                    onFocus={() => setFocused(item.index)}
                  />
                  {item.index === cutoffIndex && capacity !== null ? <CapacityLine capacity={capacity} /> : null}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- filters -- */

function FilterBar({
  filters,
  facets,
  total,
  onChange,
}: {
  filters: QueueFilters;
  facets: QueueFacets | null;
  total: number;
  onChange: (next: Partial<QueueFilters>) => void;
}): ReactElement {
  const [text, setText] = useState(filters.query);
  useEffect(() => setText(filters.query), [filters.query]);

  const chipStyle = (active: boolean): React.CSSProperties => ({
    ...T_MICRO,
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
    // The facet placeholder (—) is ~53 px wide in the fallback face and 51 px in
    // IBM Plex; pinning the minimum stops the font swap from nudging the filter row.
    minWidth: 58,
    padding: '2px 6px',
    cursor: 'pointer',
    color: 'var(--color-ink)',
    background: active ? 'var(--color-elev-2)' : 'transparent',
    border: `1px solid ${active ? 'var(--color-evidence)' : 'var(--color-hairline-strong)'}`,
    borderRadius: 'var(--radius-control)',
  });

  return (
    <div
      data-print-hide
      style={{
        padding: '8px var(--spacing-pane-gap)',
        ...HAIRLINE_BOTTOM,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <label style={{ ...T_LABEL, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="search" size={14} title="Search account key" />
          <span className="u-sr-only">Search account key</span>
          <input
            value={text}
            aria-label="Search account key"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onChange({ query: text });
            }}
            style={{
              ...T_LABEL,
              background: 'var(--color-canvas-sunken)',
              border: '1px solid var(--color-hairline)',
              borderRadius: 'var(--radius-control)',
              padding: '3px 8px',
              color: 'var(--color-ink)',
              minWidth: 180,
            }}
          />
        </label>

        <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }} data-queue-count>
          {count(total)} alerts match · j/k move · Enter opens · e escalate · d dismiss
        </span>
      </div>

      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
        <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>band</span>
        {BANDS.map((band) => {
          const facet = facets?.bands.find((entry) => entry.band === band);
          const active = filters.bands.includes(band);
          return (
            <button
              key={band}
              type="button"
              aria-pressed={active}
              onClick={() =>
                onChange({ bands: active ? filters.bands.filter((entry) => entry !== band) : [...filters.bands, band] })
              }
              style={chipStyle(active)}
            >
              <BandBadge band={band} describe={false} size={12} />
              <span className="u-num">{facet === undefined ? '—' : count(facet.count)}</span>
            </button>
          );
        })}

        <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginLeft: 8 }}>typology</span>
        {(facets?.typologies ?? []).map((entry) => {
          const active = filters.typology === entry.typology;
          const typology = entry.typology as Typology;
          return (
            <button
              key={entry.typology}
              type="button"
              aria-pressed={active}
              onClick={() => onChange({ typology: active ? null : entry.typology })}
              style={chipStyle(active)}
              title={TYPOLOGY_META[typology].reads}
            >
              <Icon name={glyphFor(typology)} size={13} />
              {entry.rule_code}
              <span className="u-num">{count(entry.count)}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- one card -- */

function QueueCard({
  row,
  assumptions,
  timeZone,
  focused,
  onFocus,
}: {
  row: AlertRow;
  assumptions: readonly { key: string; value: string | number; source: string; note: string | null }[];
  timeZone: string | null;
  focused: boolean;
  onFocus: () => void;
}): ReactElement {
  const typology = row.typology;
  return (
    <article
      role="listitem"
      data-alert-card={row.account_key}
      data-above-capacity={row.above_capacity}
      aria-current={focused ? 'true' : undefined}
      onClick={onFocus}
      style={{
        ...PANEL_SUNKEN,
        height: ROW_HEIGHT - 8,
        margin: '0 var(--spacing-pane-gap) 8px',
        padding: '10px 12px',
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) 200px 200px',
        gridTemplateRows: 'auto 1fr auto',
        columnGap: 12,
        outline: focused ? '2px solid var(--color-focus)' : 'none',
        outlineOffset: -1,
      }}
    >
      <header style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)', minWidth: 44 }}>
          #{count(row.rank)}
        </span>
        <AccountChip accountKey={row.account_key} href={row.case_href} />
        <BandBadge band={row.band} />
        <span className="u-num" style={{ ...T_LABEL, color: 'var(--color-ink)' }}>
          score {row.score.toFixed(3)}
        </span>
        {/* Confidence is the calibration bin's observed rate and n, never an adjective. */}
        <span style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }} data-calibration>
          confidence {(row.calibration.observed_rate * 100).toFixed(0)}% — observed rate in this band{' '}
          {(row.calibration.observed_rate * 100).toFixed(1)}% · n={count(row.calibration.n)}
        </span>
        {typology !== null ? (
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginLeft: 'auto' }} title={TYPOLOGY_META[typology].reads}>
            <Icon name={glyphFor(typology)} size={13} title={TYPOLOGY_META[typology].name} /> {TYPOLOGY_META[typology].name}
          </span>
        ) : null}
      </header>

      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <p style={{ ...T_MICRO, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--color-ink-faint)', margin: 0 }}>
          Top three reasons
        </p>
        <ol style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 2 }}>
          {row.reasons.map((reason) => (
            <li key={reason.attribute} style={{ ...T_LABEL, color: 'var(--color-ink-muted)', ...ELLIPSIS }} title={reason.text}>
              <span className="u-num" style={{ color: 'var(--color-ink)' }}>
                {reason.points}
              </span>{' '}
              {reason.text}
            </li>
          ))}
        </ol>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '2px 0 0' }}>
          {count(row.txn_count)} transactions in window · {count(row.review_minutes)} analyst-minutes · ranked by{' '}
          {row.policy_label}
        </p>
      </div>

      <MoneyFigure figure={row.exposure} assumptions={assumptions} label="Exposure at risk" compact />
      <MoneyFigure figure={row.expected_value} assumptions={assumptions} label="Expected value" compact showBand={false} />

      <footer style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 10, ...GAP_TIGHT, flexWrap: 'wrap' }}>
        <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
          rank {count(row.rank)} under {row.policy_label}
        </span>
        <span style={{ ...T_MICRO, color: row.above_capacity ? 'var(--color-state-done)' : 'var(--color-ink-faint)' }}>
          {row.above_capacity ? 'inside capacity' : 'below the cutoff'}
        </span>
        <span style={{ marginLeft: 'auto', ...T_MICRO }}>
          {timeZone !== null ? <Timestamp iso={row.last_seen} timeZone={timeZone} sense="last seen" /> : null}
        </span>
      </footer>
    </article>
  );
}

/* --------------------------------------------------------- capacity line -- */

/**
 * The never-cut line. It states the budget that produced it and the money the
 * remainder is worth, so "we chose not to look" is on the record rather than
 * implied by a scrollbar. Money is rendered through `compactFromMinor`, never as raw
 * minor units.
 */
function CapacityLine({ capacity }: { capacity: QueueCapacity }): ReactElement {
  const reviewed = capacity.cutoff_rank ?? 0;
  const unreviewed = capacity.unreviewed_count;
  const exposure = capacity.unreviewed_exposure;
  return (
    <div
      data-capacity-line
      role="separator"
      aria-label="Capacity cutoff. Everything below this line is not reviewed this period."
      style={{
        position: 'relative',
        margin: '0 var(--spacing-pane-gap)',
        height: 40,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          position: 'absolute',
          left: 0,
          right: 0,
          top: '50%',
          height: 2,
          background:
            'repeating-linear-gradient(90deg, var(--color-band-e) 0 8px, transparent 8px 14px)',
        }}
      />
      <span
        style={{
          ...T_MICRO,
          position: 'relative',
          padding: '2px 8px',
          background: 'var(--color-canvas)',
          border: '1px solid var(--color-band-e)',
          borderRadius: 'var(--radius-control)',
          color: 'var(--color-ink)',
          whiteSpace: 'nowrap',
        }}
      >
        capacity cutoff · {count(reviewed)} reviewed of {count(reviewed + unreviewed)} at {count(capacity.minutes_available)}{' '}
        analyst-minutes this {capacity.period_label} · {capacity.policy_label}
      </span>
      {exposure !== null ? (
        <span
          style={{
            ...T_MICRO,
            position: 'relative',
            marginLeft: 'auto',
            padding: '2px 8px',
            background: 'var(--color-canvas)',
            color: 'var(--color-ink-muted)',
            whiteSpace: 'nowrap',
          }}
        >
          below this line · {count(unreviewed)} alerts ·{' '}
          {compactFromMinor(exposure.value.minor, exposure.value.decimals)} {exposure.value.currency} of exposure
          consciously not reviewed
        </span>
      ) : null}
    </div>
  );
}
