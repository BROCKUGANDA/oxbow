/**
 * §14 `test_four_empty_states_distinct` — five named empties (the fifth is
 * `no-cycles`, the graph's own reason), none of which says "No data".
 *
 * What the plan requires of each is asserted as copy: the narrowest predicate with
 * the count that would return; the literal `uv run oxbow pipeline` command with a
 * copy button and expected runtime; the window explanation with inline widening;
 * the disagreement framed as a finding with the near-miss threshold; the cycle
 * emptiness attributed to the time-respecting filter, not to an absence of data.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EmptyState } from '@/design/primitives/EmptyState';
import { PIPELINE_COMMAND, RUNTIME_ESTIMATE_FALLBACK } from '@/lib/copy';
import { cleanupAll, click, render, text } from '@/test/render';

afterEach(cleanupAll);

const NO_DATA_PHRASE = /no data( available)?/i;

function propsCatalogue() {
  const noop = (): undefined => undefined;
  return {
    'filters-excluded': (
      <EmptyState
        kind="filters-excluded"
        unfilteredRows={1_412}
        narrowest={{ label: 'Typology is circular transfer', value: 'R4 only', rowsIfRemoved: 41, onRemove: noop }}
        others={[{ label: 'Band', value: 'E', onRemove: noop }]}
      />
    ),
    'no-run': (
      <EmptyState kind="no-run" command={PIPELINE_COMMAND} expectedRuntime={RUNTIME_ESTIMATE_FALLBACK} corpus="IBM-AML HI-Small and PaySim" />
    ),
    'window-empty': (
      <EmptyState
        kind="window-empty"
        accountId="ACC-00DORM"
        from="2026-09-11"
        to="2026-09-18"
        edgesAtCurrentHops={0}
        edgesAtWiderWindow={37}
        currentHops={1}
        maxHops={4}
        onWidenHops={noop}
        onWidenDates={noop}
      />
    ),
    'no-disagreement': (
      <EmptyState
        kind="no-disagreement"
        bandAbove="C"
        comparedAccounts={4_918}
        maxDelta={0}
        nearMissThreshold={0.15}
        rowsAtThreshold={37}
        onSetThreshold={noop}
      />
    ),
    'no-cycles': (
      <EmptyState
        kind="no-cycles"
        accountsDrawn={212}
        edgesDrawn={388}
        windowFrom="2026-09-01"
        windowTo="2026-09-29"
        currentHops={1}
        maxHops={4}
        onWidenHops={noop}
        onShowAllEdges={noop}
      />
    ),
  } as const;
}

describe('the five empty states', () => {
  it('are five distinct kinds, none of which says "No data"', () => {
    const headlines: string[] = [];
    for (const element of Object.values(propsCatalogue())) {
      const view = render(element);
      const body = text(view.container);
      expect(body).not.toMatch(NO_DATA_PHRASE);
      headlines.push(body.split('\n')[0] ?? body.slice(0, 40));
      view.cleanup();
    }
    expect(new Set(headlines).size).toBe(5);
  });

  it('filters-excluded names the narrowest predicate, its removable count, and one-click removal', () => {
    const onRemove = vi.fn();
    const view = render(
      <EmptyState
        kind="filters-excluded"
        unfilteredRows={1_412}
        narrowest={{ label: 'Typology is circular transfer', value: 'R4 only', rowsIfRemoved: 41, onRemove }}
      />,
    );
    const body = text(view.container);
    expect(body).toContain('Your filters exclude all 1,412 alerts');
    expect(body).toContain('Typology is circular transfer');
    expect(body).toContain('41');
    click(view.container, 'button');
    expect(onRemove).toHaveBeenCalledTimes(1);
    view.cleanup();
  });

  it('no-run prints the literal pipeline command with a copy button and the expected runtime', () => {
    const view = render(
      <EmptyState kind="no-run" command={PIPELINE_COMMAND} expectedRuntime={RUNTIME_ESTIMATE_FALLBACK} />,
    );
    expect(PIPELINE_COMMAND).toBe('uv run oxbow pipeline');
    const body = text(view.container);
    expect(body).toContain('uv run oxbow pipeline');
    expect(body).toContain('6–9 minutes on the dev slice');
    const copyButton = Array.from(view.container.querySelectorAll('button')).find((button) =>
      (button.textContent ?? '').includes('Copy'),
    );
    expect(copyButton).toBeDefined();
    view.cleanup();
  });

  it('window-empty explains the window and offers hop and date widening inline', () => {
    const widenHops = vi.fn();
    const view = render(
      <EmptyState
        kind="window-empty"
        accountId="ACC-00DORM"
        from="2026-09-11"
        to="2026-09-18"
        edgesAtCurrentHops={0}
        edgesAtWiderWindow={37}
        currentHops={1}
        maxHops={4}
        onWidenHops={widenHops}
        onWidenDates={() => undefined}
      />,
    );
    const body = text(view.container);
    expect(body).toContain('ACC-00DORM has no counterparties between 2026-09-11 and 2026-09-18');
    expect(body).toContain('window');
    expect(body).toContain('37');
    click(view.container, 'button');
    expect(widenHops).toHaveBeenCalledWith(2);
    view.cleanup();
  });

  it('no-disagreement frames zero disagreements as a finding with the threshold that would surface near-misses', () => {
    const view = render(
      <EmptyState
        kind="no-disagreement"
        bandAbove="C"
        comparedAccounts={4_918}
        maxDelta={0}
        nearMissThreshold={0.15}
        rowsAtThreshold={37}
        onSetThreshold={() => undefined}
      />,
    );
    const body = text(view.container);
    expect(body).toContain('The two models agree on every account above band C');
    expect(body).toContain('That is a finding');
    expect(body).toContain('0.15');
    expect(body).toContain('37');
    view.cleanup();
  });

  it('no-cycles names the time-respecting filter that emptied the window — the fifth empty state', () => {
    const view = render(
      <EmptyState
        kind="no-cycles"
        accountsDrawn={212}
        edgesDrawn={388}
        windowFrom="2026-09-01"
        windowTo="2026-09-29"
        currentHops={1}
        maxHops={4}
        onWidenHops={() => undefined}
        onShowAllEdges={() => undefined}
      />,
    );
    const body = text(view.container);
    expect(body).toContain('No cycle survived the time-respecting filter');
    expect(body).toContain('212');
    expect(body).toContain('388');
    expect(body).toContain('measurement of this corpus');
    view.cleanup();
  });
});
