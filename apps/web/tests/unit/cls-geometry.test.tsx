/**
 * §14 `test_cls_zero` — the structural half: the skeleton's geometry is the
 * resolved layout's geometry, in constants a test can hold, not in pixels a
 * browser has to forgive. The measured half (a real PerformanceObserver run over
 * /alerts and a case route) lives in the Playwright suite.
 *
 * What makes CLS zero is two invariants:
 * 1. every skeleton row is exactly the row height the resolved content uses, and
 *    the row count matches the page size the query will return;
 * 2. a pane reserves the same box for the pending state, the resolved state and the
 *    failed state, so resolution — including failure — cannot move anything below it.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { Pane } from '@/components/Pane';
import { Skeleton } from '@/design/primitives/Skeleton';
import type { ListMeta } from '@/lib/api/contract';

import { cleanupAll, mustFindAll, render } from '@/test/render';

afterEach(cleanupAll);

/** A meta that says "the query answered". `Pane` reads `meta === null` as pending, so an
 * omitted prop defaults to null and the resolved arm would have been measured as a
 * skeleton — which is the failure this file first reported as a product defect. */
const RESOLVED_META: ListMeta = {
  run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
  trace_id: '01J4Z7M2QK9N7V1C4X6E8G0B2E',
  model_version: 'scorecard-woe-v1',
  provenance: 'pipeline',
  generated_at: '2026-09-24T06:12:00Z',
  assumptions: [],
  degraded: false,
  degraded_reason: null,
  disclaimer: 'research prototype',
  limit: 8,
  offset: 0,
  total: 8,
  next_offset: null,
  sort: 'rank',
  order: 'desc',
};

const QUEUE_COLUMNS = [
  { key: 'account', width: '160px' },
  { key: 'band', width: '120px' },
  { key: 'reasons', width: 'minmax(0, 1fr)' },
  { key: 'exposure', width: '210px', align: 'end' as const },
  { key: 'ev', width: '210px', align: 'end' as const },
];

describe('matched-geometry skeletons', () => {
  it('draws exactly `rows` rows at exactly `rowHeight` — the queue promise, kept', () => {
    const view = render(
      <Skeleton label="Loading the alert queue" rows={6} rowHeight={172} showHeader={false} columns={QUEUE_COLUMNS} />,
    );
    const rows = mustFindAll<HTMLDivElement>(view.container, '[role="status"] > div[aria-hidden="true"]');
    expect(rows).toHaveLength(6);
    for (const row of rows) {
      expect(row.style.height).toBe('172px');
      expect(row.style.gridTemplateColumns).toBe(QUEUE_COLUMNS.map((column) => column.width).join(' '));
    }
    view.cleanup();
  });

  it('with a header, reserves header + rows — the full column height, announced', () => {
    const view = render(
      <Skeleton label="Loading the transaction table" rows={6} rowHeight={36} columns={QUEUE_COLUMNS} />,
    );
    const status = view.container.querySelector('[role="status"]');
    expect(status).not.toBeNull();
    expect(status?.getAttribute('aria-busy')).toBe('true');
    const heights = mustFindAll<HTMLDivElement>(view.container, 'div[aria-hidden="true"]').map(
      (row) => row.style.height,
    );
    expect(heights[0]).toBe('32px'); // the default header height, part of the reserved box
    expect(heights.filter((h) => h === '36px')).toHaveLength(6);
    view.cleanup();
  });

  it('scales to the stress page the queue asks for (1,500 rows at matched height)', () => {
    const view = render(
      <Skeleton
        label="Loading a 1,500-row queue"
        rows={1500}
        rowHeight={172}
        showHeader={false}
        columns={QUEUE_COLUMNS}
      />,
    );
    // Rows are the aria-hidden children of the status root; the cells inside them are
    // aria-hidden too, so the loose selector this test first used counted 9,000
    // elements for a 1,500-row skeleton and "failed" against a correct component.
    const rows = mustFindAll<HTMLDivElement>(view.container, 'div[role="status"] > div[aria-hidden="true"]');
    expect(rows).toHaveLength(1500);
    // The reserved height is linear in the row count: the virtualiser's totalSize is
    // count × estimateSize, so the scroll box never re-sizes on resolution.
    const reserved = rows.reduce((sum, row) => sum + Number.parseInt(row.style.height, 10), 0);
    expect(reserved).toBe(1500 * 172);
    view.cleanup();
  });
});

describe('pane reserved geometry across all three states', () => {
  const skeleton = { columns: [{ key: 'row', width: '100%' }], rows: 8, rowHeight: 36 };

  function bodyOf(view: { container: HTMLElement }): HTMLElement {
    const section = view.container.querySelector('[data-pane="scorecard-strip"]');
    if (section === null) throw new Error('pane missing');
    return section.querySelectorAll(':scope > div')[0] as HTMLElement;
  }

  it('pending, resolved and failed occupy the same reserved box', () => {
    const pending = render(
      <Pane
        id="scorecard-strip"
        title="Strip"
        operation="Loading the strip"
        skeleton={skeleton}
        reserveHeight={320}
        meta={null}
      >
        <span />
      </Pane>,
    );
    expect(bodyOf(pending).style.minHeight).toBe('320px');
    pending.cleanup();

    const resolved = render(
      <Pane
        id="scorecard-strip"
        title="Strip"
        operation="Loading the strip"
        skeleton={skeleton}
        reserveHeight={320}
        meta={RESOLVED_META}
      >
        <p>the answer</p>
      </Pane>,
    );
    expect(bodyOf(resolved).style.minHeight).toBe('320px');
    expect(bodyOf(resolved).textContent).toContain('the answer');
    resolved.cleanup();

    const failed = render(
      <Pane
        id="scorecard-strip"
        title="Strip"
        operation="Loading the strip"
        skeleton={skeleton}
        reserveHeight={320}
        failure={{
          kind: 'network',
          class: 'network',
          message: 'socket hang up',
          status: null,
          run_id: null,
          retry_after_ms: null,
        }}
        onRetry={() => undefined}
      >
        <span />
      </Pane>,
    );
    expect(bodyOf(failed).style.minHeight).toBe('320px');
    failed.cleanup();
  });
});
