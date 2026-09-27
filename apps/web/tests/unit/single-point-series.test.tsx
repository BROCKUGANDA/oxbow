/**
 * §14 `test_single_point_series` — the minimum-series guard renders an explanatory
 * note, not an absurd chart: one point is a fact and zero is an absence; neither
 * gets an axis, because an axis implies a distribution the response did not send.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { ChartFrame, LineChart, Waterfall, minimumSeriesNote } from '@/components/charts/charts';
import { cleanupAll, render, text } from '@/test/render';

afterEach(cleanupAll);

describe('the minimum-series guard', () => {
  it('answers null above one point and a note at one and zero', () => {
    expect(minimumSeriesNote(2, 'line')).toBeNull();
    expect(minimumSeriesNote(1, 'line')).toContain('One point was returned');
    expect(minimumSeriesNote(0, 'line')).toContain('no points');
    expect(minimumSeriesNote(1, 'bar')).toContain('single bar is a count');
  });

  it('a single-point line prints the value and draws no plot', () => {
    const view = render(
      <LineChart
        points={[{ x: '2026-09-01T00:00:00Z', y: 412_000_000, label: 'one fold' }]}
        yIsMoney
        ariaLabel="Single point series"
      />,
    );
    const body = text(view.container);
    expect(body).toContain('One point was returned');
    expect(body).toContain('the value is printed instead of plotted');
    expect(view.container.querySelector('svg[data-chart="line"]')).toBeNull();
    view.cleanup();
  });

  it('an empty waterfall is the same refusal, and the guard note keeps chart height', () => {
    const view = render(<Waterfall rows={[]} ariaLabel="Empty waterfall" />);
    expect(text(view.container)).toContain('no points');
    view.cleanup();

    const framed = render(
      <ChartFrame height={220} note={minimumSeriesNote(0, 'line') ?? ''} label="empty series guard" />,
    );
    const frame = framed.container.querySelector('[data-chart-note]');
    expect(frame).not.toBeNull();
    expect((frame as HTMLElement).style.height).toBe('220px');
    framed.cleanup();
  });

  it('two points are a line, and the guard steps aside', () => {
    const view = render(
      <LineChart
        points={[
          { x: '2026-09-01T00:00:00Z', y: 100, label: null },
          { x: '2026-09-02T00:00:00Z', y: 200, label: null },
        ]}
        ariaLabel="Two point series"
      />,
    );
    expect(text(view.container)).not.toContain('One point was returned');
    view.cleanup();
  });
});
