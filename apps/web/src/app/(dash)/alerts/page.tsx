/* =============================================================================
   Alert queue — plan §14 P8a-2.

   THE CAPACITY LINE. Everything below it is what this period is consciously not
   reviewed. It is on the never-cut list because it is the argument: a ranked list
   implies the work gets done, and the honest statement is that a budget decides
   where reading stops. So it is not a hair and a label — it spans the list and
   carries the count beneath it, the minutes that bought it, and the money the
   remainder is worth, all three from the response.

   VIRTUALISATION. Rows are windowed by TanStack Virtual against `ROW_HEIGHT`, the same
   constant the skeleton and the card itself are built from, inside a body region whose
   height is the viewport minus the shell chrome (`--shell-chrome-height`) and never the
   response. That is how CLS stays at zero on a ten-thousand-row queue rather than merely
   getting small: the box the rows arrive in already exists, in the pending state, in the
   empty state and in the failed one, so nothing any response can say moves the page.

   URL SYNC. Filter, page, sort and focus are the query string, so
   `/alerts?band=E&typology=R4&offset=1200` reopens exactly what an analyst was
   reading. Filtering is server-side: the count an empty state quotes has to be the
   server's count, not a client-side survivors tally.

   KEYBOARD. j/k move, Enter opens, e escalates, d dismisses. Bound at the window so
   both hands stay off the mouse while reading; the route each key pushes is the same
   route a click would push.
   ============================================================================= */

'use client';

import { useVirtualizer } from '@tanstack/react-virtual';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { type ReactElement, type ReactNode, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { TYPOLOGY_META, glyphFor } from '@/components/typology';
import { BandBadge } from '@/components/ui/BandBadge';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { AccountChip, RunIdChip, Timestamp } from '@/components/ui/provenance';
import { ELLIPSIS, GAP_TIGHT, HAIRLINE_BOTTOM, PANEL_SUNKEN, T_LABEL, T_MICRO } from '@/components/ui/sx';
import { Icon } from '@/design/icons/Icon';
import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { Shimmer } from '@/design/primitives/Shimmer';
import { Skeleton } from '@/design/primitives/Skeleton';
import {
  type AlertRow,
  type Band,
  type QueueCapacity,
  type QueueFacets,
  ROUTES,
  type Typology,
} from '@/lib/api/contract';
import { useListResource, useRuntime } from '@/lib/api/hooks';
import { failureDetail, failureRunId, failureTitle, isRunNotFound } from '@/lib/api/problem';
import { PIPELINE_COMMAND, RUNTIME_ESTIMATE_FALLBACK } from '@/lib/copy';
import { count } from '@/lib/format/money';

/** THE queue row box: the virtualiser's estimate, the skeleton's row height and the
 *  card's own outer height are this one number, in that one place. It is derived from
 *  the measured card rather than asserted: the resolved card at a 1440 px viewport is
 *  10 padding + 27 header + 128 the reason/money row (the 128 is what the assumption
 *  line and recovery band DESIGN.md §7 obliges the money column to carry) + 14 footer
 *  + 10 padding = 189, plus the 8 px gutter between cards. The number in the fixture
 *  was 172, and the 17 px it lost was printing the card footer over the next card's
 *  border. Geometry, not data. */
const ROW_GUTTER = 8;
const CARD_HEIGHT = 189;
const ROW_HEIGHT = CARD_HEIGHT + ROW_GUTTER;

/** The capacity line's own row. It is a slot in the virtualised list rather than a
 *  child of a card, so it gets this much real space and nothing overlaps it. */
const CAPACITY_LINE_HEIGHT = 40;

/** One entry in the virtualised list: a queue row, or the capacity line between rows. */
type QueueSlot = { kind: 'row'; row: AlertRow } | { kind: 'capacity'; capacity: QueueCapacity };

/** The filter bar's own box, fixed for the same reason: the facet chips it renders are
 *  server-determined, so an auto-height bar grows when a response arrives and every
 *  row below it moves. The bar and the two reserves that stand in for it (the suspense
 *  fallback and nothing else now — the failed arm renders the real bar) all read this. */
const FILTER_BAR_HEIGHT = 68;
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
  /* The suspense fallback and the resolved-but-pending queue must be the same boxes:
     this boundary is crossed on every load, so a bar slot that is 24 px shorter than
     the real filter bar moves the whole list. `QueueFrame` owns the chrome and the
     body region, and both arms put a matched-geometry skeleton in it. */
  return (
    <QueueFrame barSlot={<FilterBarSkeleton />}>
      <QueueRowsSkeleton />
    </QueueFrame>
  );
}

/** The body region every arm shares: the filter bar on top, then one flexed region
 *  that is the same box whether it is holding a skeleton, the virtualised rows, an
 *  empty state or the error tier. DESIGN.md §5 makes resolution — including failure,
 *  which is a resolved state — cost zero layout shift, and the only way that is true
 *  rather than merely small is for the arms to share one frame. */
function QueueFrame({ barSlot, children }: { barSlot: ReactNode; children: ReactNode }): ReactElement {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: 'calc(100vh - var(--shell-chrome-height))',
      }}
    >
      {barSlot}
      <div data-queue-body style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {children}
      </div>
    </div>
  );
}

/** The queue body's own padding box, identical in the pending, empty and failed arms so
 *  swapping one for the other cannot move the frame. The resolved list scrolls inside
 *  its own region instead (`data-queue-scroll`). */
function QueueBody({ children, pad = true }: { children: ReactNode; pad?: boolean }): ReactElement {
  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        overflowY: 'auto',
        padding: pad ? 'var(--spacing-pane-gap)' : '0 var(--spacing-pane-gap)',
      }}
    >
      {children}
    </div>
  );
}

function QueueRowsSkeleton(): ReactElement {
  return (
    <QueueBody pad={false}>
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
    </QueueBody>
  );
}

/** A shimmer in the filter bar's exact box. The bar is real chrome rather than a
 *  reserved gap wherever the query has a shape to describe; this stands in only for
 *  the suspense crossing, where the query string — and so the filter state — is not
 *  yet readable. */
function FilterBarSkeleton(): ReactElement {
  return (
    <div
      aria-hidden="true"
      data-filter-bar-skeleton
      style={{
        height: FILTER_BAR_HEIGHT,
        boxSizing: 'border-box',
        padding: '8px var(--spacing-pane-gap)',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        ...HAIRLINE_BOTTOM,
      }}
    >
      <div style={{ height: 22, display: 'flex', alignItems: 'center' }}>
        <Shimmer width={260} height={22} radius="var(--radius-control)" />
      </div>
      <div style={{ height: 24, display: 'flex', alignItems: 'center' }}>
        <Shimmer width={520} height={22} radius="var(--radius-control)" />
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
  /** The server's count, or null while no response has carried one. `0` is a number the
   *  API never sent, and DESIGN.md §7 forbids rendering it: while the queue is in flight
   *  — or has failed — the filter bar says `—`, the same placeholder the facet chips use. */
  const total = queue.meta?.total ?? null;

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

  /** Index of the row the cutoff is drawn after, or -1 when it is off-page. */
  const cutoffIndex = useMemo(() => {
    if (capacity === null || capacity.cutoff_rank === null) return -1;
    const index = rows.findIndex((row) => row.rank === capacity.cutoff_rank);
    if (index >= 0) return index;
    // The cutoff sits between two pages: draw it on whichever side it is nearest so
    // the line never silently disappears from a filtered view.
    return capacity.cutoff_rank < (rows[0]?.rank ?? 0) ? 0 : -1;
  }, [rows, capacity]);

  /* The queue is virtualised over *slots*, not rows: the capacity line is a slot with
     its own measured height, because as a child of a fixed-height row it overflowed
     into the next card's header — the one line in this route that must not overlap the
     evidence it is dividing. A slot list keeps `estimateSize` exact from the first
     paint, which is what the zero-shift promise needs: the virtualiser never has to
     correct a row it already placed. */
  const slots = useMemo<QueueSlot[]>(() => {
    if (cutoffIndex < 0) return rows.map((row): QueueSlot => ({ kind: 'row', row }));
    const out: QueueSlot[] = [];
    rows.forEach((row, index) => {
      out.push({ kind: 'row', row });
      if (index === cutoffIndex && capacity !== null) out.push({ kind: 'capacity', capacity });
    });
    return out;
  }, [rows, cutoffIndex, capacity]);

  /** Row index ↔ slot index, so `j`/`k` move through rows and skip the capacity line. */
  const { rowSlot, slotRow } = useMemo(() => {
    const rowsToSlots: number[] = [];
    const slotsToRows: (number | null)[] = [];
    for (const slot of slots) {
      if (slot.kind === 'row') {
        rowsToSlots.push(slotsToRows.length);
        slotsToRows.push(rowsToSlots.length - 1);
      } else {
        slotsToRows.push(null);
      }
    }
    return { rowSlot: rowsToSlots, slotRow: slotsToRows };
  }, [slots]);

  const virtualiser = useVirtualizer({
    count: slots.length,
    getScrollElement: () => listRef.current,
    estimateSize: (index) => (slots[index]?.kind === 'capacity' ? CAPACITY_LINE_HEIGHT : ROW_HEIGHT),
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
        virtualiser.scrollToIndex(rowSlot[next] ?? 0);
        return;
      }
      const row = rows[focused];
      if (row === undefined) return;
      // A row with no case has nowhere to open and nothing to decide. The shortcut is
      // ignored rather than routing to `/cases/null`, which is what an unconditional push
      // on the old non-nullable `case_href` would have become the moment the server told
      // the truth about an account nobody had opened yet.
      if (row.case_href === null) return;
      if (event.key === 'Enter') router.push(row.case_href);
      if (event.key === 'e' || event.key === 'd') {
        router.push(`${row.case_href}?decide=${event.key === 'e' ? 'escalate' : 'dismiss'}`);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [rows, focused, router, virtualiser, rowSlot]);

  /* Four arms, one frame. The filter bar is real chrome in all of them — including the
     failed one, where the analyst still has a band and a typology to change before
     retrying — and the body region under it is the same box in all of them, so neither
     resolution nor failure can move anything. This is DESIGN.md §5's "skeleton geometry
     matches the resolved layout exactly" measured, not asserted: the mover the Playwright
     probe named on this route was the body div jumping 24 px when the failed arm swapped
     the 68 px filter bar for a 44 px reserved spacer. A 503 is a resolved state. */
  const failure = queue.failure;

  return (
    <QueueFrame
      barSlot={
        <FilterBar filters={filters} facets={queue.data?.facets ?? null} total={total} onChange={writeFilters} />
      }
    >
      {failure !== null && queue.data === null ? (
        <QueueBody>
          {isRunNotFound(failure) ? (
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
                title: failureTitle(failure),
                detail: failureDetail(failure) ?? undefined,
                run_id: failureRunId(failure) ?? undefined,
              }}
              onRetry={() => void queue.refetch()}
              attempt={queue.attempts}
              retrying={queue.isFetching}
              siblingsIntact={false}
            >
              <RunIdChip runId={failureRunId(failure)} />
            </ErrorPane>
          )}
        </QueueBody>
      ) : queue.data === null ? (
        <QueueRowsSkeleton />
      ) : rows.length === 0 ? (
        <QueueBody>
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
                  writeFilters(
                    which === 'typology' ? { typology: null } : which === 'band' ? { bands: [] } : { query: '' },
                  );
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
        </QueueBody>
      ) : (
        <div
          ref={listRef}
          data-queue-scroll
          className="u-scroll"
          style={{ flex: 1, minHeight: 0, position: 'relative' }}
          role="list"
          aria-label={`Alert queue. ${count(rows.length)} of ${total === null ? '—' : count(total)} alerts match the active policy`}
        >
          <div style={{ height: virtualiser.getTotalSize(), position: 'relative' }}>
            {virtualiser.getVirtualItems().map((item) => {
              const slot = slots[item.index];
              if (slot === undefined) return null;
              const isCapacity = slot.kind === 'capacity';
              return (
                <div
                  key={isCapacity ? 'capacity-line' : slot.row.account_key}
                  data-index={item.index}
                  ref={virtualiser.measureElement}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${item.start}px)`,
                    height: isCapacity ? CAPACITY_LINE_HEIGHT : ROW_HEIGHT,
                  }}
                >
                  {isCapacity ? (
                    <CapacityLine capacity={slot.capacity} />
                  ) : (
                    <QueueCard
                      row={slot.row}
                      assumptions={assumptions}
                      timeZone={timeZone}
                      focused={slotRow[item.index] === focused}
                      onFocus={() => {
                        const rowIndex = slotRow[item.index];
                        if (rowIndex !== null && rowIndex !== undefined) setFocused(rowIndex);
                      }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </QueueFrame>
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
  total: number | null;
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
    // The rail scrolls rather than wraps, and a chip that shrank to fit would change
    // the rail's content width and re-wrap nothing — it would just become unreadable.
    flexShrink: 0,
    height: 22,
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
      data-queue-bar
      style={{
        padding: '8px var(--spacing-pane-gap)',
        ...HAIRLINE_BOTTOM,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        /* Declared, not derived. The facets this bar renders are server-determined, so
           an auto-height bar grows the moment a response arrives and every row under it
           moves — which is the shift DESIGN.md §5 forbids. Both rows are therefore fixed
           and the facet rail scrolls sideways instead of wrapping into a third line. */
        height: FILTER_BAR_HEIGHT,
        boxSizing: 'border-box',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, height: 22, flexShrink: 0 }}>
        <label style={{ ...T_LABEL, display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
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

        <span
          style={{ ...T_MICRO, color: 'var(--color-ink-faint)', ...ELLIPSIS }}
          data-queue-count
          title={`${total === null ? 'No count reported yet' : `${count(total)} alerts match`} · j/k move · Enter opens · e escalate · d dismiss`}
        >
          {/* `—` until the response carries a total. A pending or failed queue printing
              0 would be a number no API response sent (§7), and it is the same placeholder
              the facet chips already use. */}
          {total === null ? '—' : count(total)} alerts match · j/k move · Enter opens · e escalate · d dismiss
        </span>
      </div>

      <div
        className="u-scroll-x"
        style={{ height: 24, display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'nowrap' }}
      >
        <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)', flexShrink: 0 }}>band</span>
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

        <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginLeft: 8, flexShrink: 0 }}>typology</span>
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

/**
 * The response's own `allocation_source`, in the words the queue is allowed to use.
 * `stored` means the run wrote `policy_allocation` rows; `reallocated` means the API drew
 * the line just now from stored economics. The old card printed a `policy_label` the API
 * never serves, which on a run with no active policy rendered as "ranked by undefined".
 */
function rankedByLabel(source: string): string {
  if (source === 'stored') return 'the run\'s stored policy allocation';
  if (source === 'reallocated') return 'a live re-allocation over stored economics';
  return source;
}

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
      data-selected={row.selected}
      data-calibration-kind={row.confidence.kind}
      aria-current={focused ? 'true' : undefined}
      onClick={onFocus}
      style={{
        ...PANEL_SUNKEN,
        height: CARD_HEIGHT,
        margin: `0 var(--spacing-pane-gap) ${ROW_GUTTER}px`,
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
        {/* Confidence is the calibration bin's observed rate and n, never an adjective —
            and when the fold refused to calibrate, it is that refusal instead of a rate.
            The uncalibrated arm is the state of the landed run on all 43,046 rows: 18
            validation positives is below `min_positives_for_calibration`, so no observed
            rate exists to show and the card says so. */}
        {row.confidence.kind === 'calibrated' ? (
          <span style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }} data-calibration>
            confidence {(row.confidence.observed_rate * 100).toFixed(0)}% — observed rate in this band{' '}
            {(row.confidence.observed_rate * 100).toFixed(1)}% · n={count(row.confidence.n)}
          </span>
        ) : (
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }} data-calibration title={row.confidence.note}>
            probability uncalibrated — no observed rate measured
          </span>
        )}
        {typology !== null ? (
          <span
            style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginLeft: 'auto' }}
            title={TYPOLOGY_META[typology].reads}
          >
            <Icon name={glyphFor(typology)} size={13} title={TYPOLOGY_META[typology].name} />{' '}
            {TYPOLOGY_META[typology].name}
          </span>
        ) : null}
      </header>

      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <p
          style={{
            ...T_MICRO,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            color: 'var(--color-ink-faint)',
            margin: 0,
          }}
        >
          Top three reasons
        </p>
        <ol style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 2 }}>
          {row.reasons.map((reason) => (
            <li
              key={reason.text}
              style={{ ...T_LABEL, color: 'var(--color-ink-muted)', ...ELLIPSIS }}
              title={reason.text}
            >
              {/* The producer stores the sentence and, sometimes, no point value at all.
                  A missing `points` renders as no number — a `0` there would read as
                  "contributed nothing", which is a different claim from "not recorded". */}
              {reason.points !== null ? <span className="u-num" style={{ color: 'var(--color-ink)' }}>{reason.points}</span> : null}{' '}
              {reason.text}
            </li>
          ))}
        </ol>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '2px 0 0' }}>
          {count(row.txn_count)} transactions in window · ranked by {rankedByLabel(row.allocation_source)}
        </p>
      </div>

      <MoneyFigure figure={row.exposure} assumptions={assumptions} label="Exposure at risk" compact />
      <MoneyFigure
        figure={row.expected_value}
        assumptions={assumptions}
        label="Expected value"
        compact
        showBand={false}
      />

      <footer
        style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 10, ...GAP_TIGHT, flexWrap: 'wrap' }}
      >
        <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
          rank {count(row.rank)} under {rankedByLabel(row.allocation_source)}
        </span>
        <span style={{ ...T_MICRO, color: row.selected ? 'var(--color-state-done)' : 'var(--color-ink-faint)' }}>
          {row.selected ? 'funded this period' : row.beyond_capacity ? 'below the cutoff' : 'not funded'}
        </span>
        <span style={{ marginLeft: 'auto', ...T_MICRO }}>
          {/* Unconditional. `Timestamp` has an honest "zone unreported" arm for the case
              where `GET /api/meta/run` has not answered — which in this deployment is
              every screen — and a silently missing "last seen" reads as "no timestamp
              exists" rather than "the zone is unknown" (§5). */}
          <Timestamp iso={row.last_seen} timeZone={timeZone} sense="last seen" />
        </span>
      </footer>
    </article>
  );
}

/* --------------------------------------------------------- capacity line -- */

/**
 * The never-cut line. It states the budget that produced it and where the line fell, so
 * "we chose not to look" is on the record rather than implied by a scrollbar.
 *
 * What it does not state is the money sitting below the line. That figure belongs to
 * `policy_summary`, which no landed run populates, and the rule the API enforces on itself
 * — nothing composed where a measurement is absent — binds the browser too. The tile was
 * dropped rather than filled in with a sum over the page the reader happens to be looking
 * at, which would have been a different number on every scroll.
 */
function CapacityLine({ capacity }: { capacity: QueueCapacity }): ReactElement {
  const statement =
    capacity.cutoff_rank === null
      ? `nothing clears positive expected value at ${count(capacity.capacity_minutes)} analyst-minutes — every account below is unreviewed by that decision`
      : `capacity cutoff · funded through rank ${count(capacity.cutoff_rank)} of the priced run at ${count(capacity.capacity_minutes)} analyst-minutes`;
  return (
    <div
      data-capacity-line
      style={{
        position: 'relative',
        margin: '0 var(--spacing-pane-gap)',
        height: CAPACITY_LINE_HEIGHT,
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
          background: 'repeating-linear-gradient(90deg, var(--color-band-e) 0 8px, transparent 8px 14px)',
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
        {statement} · {rankedByLabel(capacity.allocation_source)}
      </span>
      {capacity.unpriced_accounts > 0 ? (
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
          {count(capacity.unpriced_accounts)} scored accounts carry no economics row, so they are not ranked at all
        </span>
      ) : null}
    </div>
  );
}
