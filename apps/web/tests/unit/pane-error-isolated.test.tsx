import type { ReactElement } from 'react';
/**
 * §14 `test_pane_error_isolated` — one failing pane must not take the page with it.
 *
 * Both paths of plan §14's pane tier are exercised on the real Pane component:
 * * the raised-render path (`react-error-boundary` inside the pane), and
 * * the failed-query path (`failure` prop from the typed union),
 * each printing the run_id with a copy button when the problem carries one, saying
 * what still works, and retrying only itself.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Pane } from '@/components/Pane';
import { ApiError } from '@/lib/api/problem';
import { cleanupAll, click, render, text } from '@/test/render';

afterEach(cleanupAll);

const RUN_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2D';

/** Throws during RENDER, which is what the pane boundary exists to catch. */
function Throwing(): ReactElement {
  throw new ApiError({
    kind: 'problem',
    class: 'server',
    problem: {
      type: 'https://oxbow.dev/problems/upstream-failure',
      title: 'Upstream failure',
      status: 502,
      detail: 'the warehouse port returned a reset mid-query',
      instance: '/api/graph/subgraph',
      run_id: RUN_ID,
      trace_id: null,
      errors: null,
      retryable: true,
      conflict: null,
    },
    retry_after_ms: null,
  });
}

/** Silence the expected boundary log without touching any behaviour. */
function withQuietConsole(fn: () => void): void {
  const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  try {
    fn();
  } finally {
    error.mockRestore();
  }
}

describe('pane error isolation', () => {
  it('a raised render fails one pane while every sibling keeps working', () => {
    withQuietConsole(() => {
      const view = render(
        <div>
          {/* `resolved={false}` for the pane whose render threw: it produced nothing this
              page may render, which is precisely the state the boundary is there to
              contain. The sibling is `resolved={true}` because its own query did answer —
              the claim under test is that one failure does not strand the other. */}
          <Pane id="failing" title="Failing" operation="Loading the network graph" resolved={false}>
            <Throwing />
          </Pane>
          <Pane id="healthy" title="Healthy" operation="Loading the ledger" resolved={true}>
            <p id="still-here">the ledger is live</p>
          </Pane>
        </div>,
      );
      const alert = view.container.querySelector('[role="alert"]');
      expect(alert).not.toBeNull();
      expect(alert?.getAttribute('data-pane')).toBe('failing');
      expect(text(view.container)).toContain('Loading the network graph failed');
      expect(text(view.container)).toContain('the warehouse port returned a reset mid-query');
      expect(text(view.container)).toContain('The rest of the page is still live.');
      expect(view.container.querySelector('#still-here')).not.toBeNull();
      view.cleanup();
    });
  });

  it('the failing pane prints the run_id with a copy button, not a stack trace', () => {
    withQuietConsole(() => {
      const view = render(
        <Pane id="failing" title="Failing" operation="Loading the network graph" resolved={false}>
          <Throwing />
        </Pane>,
      );
      const body = text(view.container);
      expect(body).toContain(RUN_ID);
      expect(body).toContain('run_id');
      expect(body).toContain('Copy');
      const copy = Array.from(view.container.querySelectorAll('button')).find((b) => (b.textContent ?? '') === 'Copy');
      expect(copy).toBeDefined();
      click(view.container, 'button:last-of-type');
      expect(body).not.toContain('at Pane');
      view.cleanup();
    });
  });

  it('a failed query without a run_id says so plainly instead of inventing one', () => {
    // `resolved={false}` is load-bearing here, not decoration: `MaybeSuspense` only routes
    // to the QueryFailure surface when the pane has NOT resolved. Set it true and this
    // test would be asserting an error surface the component correctly refuses to show.
    const view = render(
      <Pane
        id="alerts"
        title="Queue"
        operation="Loading the alert queue"
        resolved={false}
        failure={{
          kind: 'problem',
          class: 'server',
          problem: {
            type: 'https://oxbow.dev/problems/dependency-unavailable',
            title: 'Dependency unavailable',
            status: 503,
            detail: 'run 01M3F45EBQ2T63P0WNKWFC4KX7 has no priced accounts',
            instance: '/api/alerts',
            run_id: null,
            trace_id: 'f7a12dbc32f94579ba9bff2dc53812d3',
            errors: null,
            retryable: true,
            conflict: null,
          },
          retry_after_ms: null,
        }}
        onRetry={() => undefined}
        attempt={4}
      >
        <span />
      </Pane>,
    );
    const body = text(view.container);
    expect(body).toContain('No run_id');
    expect(body).toContain('attempt 4');
    expect(body).toContain('Dependency unavailable (HTTP 503)');
    expect(body).not.toMatch(/at \w+ \(/); // never a raw stack trace
    view.cleanup();
  });

  it('retry is scoped to the pane: pressing it calls the pane retry, not a reload', () => {
    const onRetry = vi.fn();
    const view = render(
      <Pane
        id="drift"
        title="Drift"
        operation="Loading the drift panel"
        resolved={false}
        failure={{
          kind: 'network',
          class: 'network',
          message: 'socket hang up',
          status: null,
          run_id: null,
          retry_after_ms: null,
        }}
        onRetry={onRetry}
      >
        <span />
      </Pane>,
    );
    const retry = Array.from(view.container.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('Retry this pane'),
    );
    expect(retry).toBeDefined();
    click(view.container, 'button');
    expect(onRetry).toHaveBeenCalledTimes(1);
    view.cleanup();
  });
});
