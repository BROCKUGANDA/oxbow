/**
 * §14 `test_stale_response_discarded` — the request-sequence guard.
 *
 * The failure it prevents is the visible one from the plan: filter the queue from
 * A to B, B answers first, and A arrives late and paints itself over the screen
 * while the control says B. The guard lives in the client (begin/isStale) and the
 * rendered demonstration lives in the gallery's StaleRow, so both are asserted here.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { begin, isStale } from '@/lib/api/client';
import { StaleRow } from '@/app/dev/states/gallery-rows';
import { cleanupAll, flush, render, text } from '@/test/render';
import { StaleResponseError } from '@/lib/api/hooks';

afterEach(cleanupAll);

describe('the sequence guard (client.ts begin/isStale)', () => {
  it('a superseded sequence reads stale, the newest never does', () => {
    const key = 'unit-queue';
    const first = begin(key);
    const second = begin(key);
    expect(second).toBe(first + 1);
    expect(isStale(key, first)).toBe(true);
    expect(isStale(key, second)).toBe(false);
  });

  it('keys are scoped per query, so one route discarding does not touch another', () => {
    const queueFirst = begin('alerts');
    begin('alerts');
    const caseSeq = begin('case:ACC-01');
    expect(isStale('case:ACC-01', caseSeq)).toBe(false);
    expect(isStale('alerts', queueFirst)).toBe(true);
  });

  it('StaleResponseError is an Error carrying the superseded sequence', () => {
    const error = new StaleResponseError('alerts', 3);
    expect(error.name).toBe('StaleResponseError');
    expect(error.message).toContain('3');
  });
});

describe('the rendered demonstration (gallery StaleRow)', () => {
  it('paints the newest answer and discards the superseded one', () => {
    vi.useFakeTimers();
    const view = render(<StaleRow />);

    // Nothing has answered yet, and the line says so rather than showing a blank cell.
    expect(text(view.container)).toContain('running…');

    // The second request resolves first — it is the newest, so it wins, and the discard
    // count is still zero because nothing superseded has come back yet.
    flush(() => {
      vi.advanceTimersByTime(50);
    });
    expect(text(view.container)).toContain('painted: second');
    expect(text(view.container)).toContain('discarded: 0');

    // The first (older) response arrives late; the guard must discard it, and the
    // rendered line must read "painted: second · discarded: 1 superseded response".
    flush(() => {
      vi.advanceTimersByTime(300);
    });
    const line = text(view.container);
    expect(line).toContain('painted: second');
    expect(line).toContain('discarded: 1 superseded response');

    view.cleanup();
    vi.useRealTimers();
  });
});
