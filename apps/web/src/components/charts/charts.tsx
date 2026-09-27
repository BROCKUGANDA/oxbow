/* =============================================================================
   Chart primitives, on visx.

   WHY visx AND NOT a chart component. A chart here is a rendering of an array the
   API sent, and the design contract's demand is that the chart's geometry is
   stable enough to hold CLS at zero and its tokens are the product's tokens. visx
   gives SVG under React's control, which means the axis label and the row height
   are the same elements before and after resolution. A canvas chart with an
   intrinsic size chosen at runtime is a layout shift waiting to happen.

   THE GUARD EVERY CHART SHARES. `minimumSeriesNote` implements
   plan §14's `test_single_point_series`: one point is not a curve and plotting it
   as one is how a chart starts lying. So each chart checks its own point count and
   renders an explanatory note at the identical box height instead of an absurd
   picture. Zero points is the same path with different words. Both keep the height
   fixed, so the guard itself does not shift the page.

   No chart here invents a domain value: extents come from the points passed in.
   ============================================================================= */

import { AxisBottom, AxisLeft } from '@visx/axis';
import { Group } from '@visx/group';
import { ParentSize } from '@visx/responsive';
import { scaleLinear } from '@visx/scale';
import { scaleLinear as bandLinear, scaleBand } from '@visx/scale';
import { Bar, LinePath } from '@visx/shape';
import { type ReactElement, type ReactNode, useState } from 'react';

import { CANVAS_RAISED, EVIDENCE, HAIRLINE, INK, INK_FAINT, INK_MUTED } from '../../design/tokens';
import type { SeriesPoint } from '../../lib/api/contract';

/** The one chart height in the product. Chosen once so a page's charts align. */
export const CHART_HEIGHT = 220;
export const CHART_HEIGHT_TALL = 300;

const MARGIN = { top: 12, right: 12, bottom: 28, left: 52 };

export type ChartProps = {
  points: readonly SeriesPoint[];
  /** y is money in minor units when true; a rate otherwise. Formats the axis only. */
  yIsMoney?: boolean;
  xLabel?: string;
  yLabel?: string;
  height?: number;
  /** The datum the operating point sits on, marked in the reserved accent. */
  mark?: { x: string; label: string } | null;
  formatY?: (value: number) => string;
  formatX?: (value: string) => string;
  ariaLabel: string;
};

export function LineChart(props: ChartProps): ReactElement {
  return (
    <div style={{ width: '100%', height: props.height ?? CHART_HEIGHT }}>
      <ParentSize>
        {({ width, height }) => <LineChartInner width={Math.max(width, 1)} height={height} {...props} />}
      </ParentSize>
    </div>
  );
}

function LineChartInner({
  width,
  height,
  points,
  mark = null,
  formatY,
  formatX,
  xLabel,
  yLabel,
  ariaLabel,
  yIsMoney = false,
}: ChartProps & { width: number; height: number }): ReactElement {
  const note = minimumSeriesNote(points.length, 'line');
  if (note !== null) return <ChartFrame height={height} note={note} label={ariaLabel} />;

  const xDomain = extent(points.map((point) => Date.parse(point.x)));
  const yDomain = extent(points.map((point) => point.y));
  const x = scaleLinear({ domain: [xDomain[0] - 1, xDomain[1] + 1], range: [MARGIN.left, width - MARGIN.right] });
  const y = scaleLinear({
    domain: [Math.min(0, yDomain[0]), yDomain[1] === yDomain[0] ? yDomain[1] + 1 : yDomain[1]],
    range: [height - MARGIN.bottom, MARGIN.top],
  });

  const tickFormat = formatY ?? defaultTick(yIsMoney);
  const xTickFormat = formatX ?? ((value: string) => value.slice(0, 10));

  return (
    <svg width={width} height={height} role="img" aria-label={ariaLabel} data-chart="line" data-points={points.length}>
      {y.ticks(4).map((tick) => (
        <line
          key={`grid-${String(tick)}`}
          x1={MARGIN.left}
          x2={width - MARGIN.right}
          y1={y(tick)}
          y2={y(tick)}
          stroke={HAIRLINE}
          strokeWidth={1}
        />
      ))}

      <AxisLeft
        scale={y}
        left={MARGIN.left}
        numTicks={4}
        tickFormat={(tick) => tickFormat(Number(tick))}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 10,
          textAnchor: 'end',
          dx: -6,
          dy: 3,
          fontFamily: 'var(--font-mono)',
        })}
      />
      <AxisBottom
        scale={x}
        top={height - MARGIN.bottom}
        numTicks={Math.max(2, Math.min(6, points.length))}
        tickFormat={(tick) => xTickFormat(new Date(Number(tick)).toISOString())}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 10,
          textAnchor: 'middle',
          dy: 6,
          fontFamily: 'var(--font-mono)',
        })}
      />

      <LinePath
        data={points as SeriesPoint[]}
        x={(point) => x(Date.parse(point.x))}
        y={(point) => y(point.y)}
        stroke={INK}
        strokeWidth={1.75}
      />

      {mark !== null ? <MarkPoint points={points} mark={mark} x={x} y={y} /> : null}

      {xLabel !== undefined ? (
        <text x={width - MARGIN.right} y={height - 2} fill={INK_FAINT} fontSize={9} textAnchor="end">
          {xLabel}
        </text>
      ) : null}
      {yLabel !== undefined ? (
        <text transform={`translate(10, ${MARGIN.top}) rotate(-90)`} fill={INK_FAINT} fontSize={9} textAnchor="end">
          {yLabel}
        </text>
      ) : null}
    </svg>
  );
}

function MarkPoint({
  points,
  mark,
  x,
  y,
}: {
  points: readonly SeriesPoint[];
  mark: { x: string; label: string };
  x: (value: number) => number;
  y: (value: number) => number;
}): ReactElement | null {
  const point = points.find((candidate) => candidate.x === mark.x);
  if (point === undefined) return null;
  const cx = x(Date.parse(point.x));
  const cy = y(point.y);
  return (
    <Group>
      <line x1={cx} x2={cx} y1={MARGIN.top} y2={cy} stroke={EVIDENCE} strokeWidth={1} strokeDasharray="2 2" />
      <circle cx={cx} cy={cy} r={4} fill={EVIDENCE} />
      <text x={cx + 6} y={cy - 6} fill={EVIDENCE} fontSize={10} fontFamily="var(--font-sans)">
        {mark.label}
      </text>
    </Group>
  );
}

/* ------------------------------------------------------------- multi ----- */

export type MultiSeriesProps = {
  series: readonly { label: string; points: readonly SeriesPoint[]; is_policy: boolean }[];
  height?: number;
  yIsMoney?: boolean;
  formatY?: (value: number) => string;
  ariaLabel: string;
};

/** One line per policy, which is how the cumulative benefit curve is meant to read:
 *  the active policy against the four baselines, on one axis. */
export function MultiLineChart({
  series,
  height = CHART_HEIGHT_TALL,
  yIsMoney = true,
  formatY,
  ariaLabel,
}: MultiSeriesProps): ReactElement {
  const longest = series.reduce((max, entry) => Math.max(max, entry.points.length), 0);
  const note = minimumSeriesNote(longest, 'line');
  if (note !== null || series.length === 0) {
    return (
      <div style={{ width: '100%', height }}>
        <ChartFrame height={height} note={note ?? 'The response contained no series.'} label={ariaLabel} />
      </div>
    );
  }

  return (
    <div style={{ width: '100%', height }}>
      <ParentSize>
        {({ width, height: boxHeight }) => (
          <svg
            width={Math.max(width, 1)}
            height={boxHeight}
            role="img"
            aria-label={ariaLabel}
            data-chart="multi-line"
            data-points={longest}
          >
            <MultiSeriesInner
              width={Math.max(width, 1)}
              height={boxHeight}
              series={series}
              yIsMoney={yIsMoney}
              formatY={formatY}
            />
          </svg>
        )}
      </ParentSize>
      <Legend series={series} />
    </div>
  );
}

function MultiSeriesInner({
  width,
  height,
  series,
  yIsMoney,
  formatY,
}: Required<Pick<MultiSeriesProps, 'series' | 'yIsMoney'>> & {
  width: number;
  height: number;
  formatY?: ((value: number) => string) | undefined;
}): ReactElement {
  const all = series.flatMap((entry) => entry.points);
  const xDomain = extent(all.map((point) => Date.parse(point.x)));
  const yDomain = extent(all.map((point) => point.y));
  const x = scaleLinear({ domain: [xDomain[0] - 1, xDomain[1] + 1], range: [MARGIN.left, width - MARGIN.right] });
  const y = scaleLinear({
    domain: [Math.min(0, yDomain[0]), yDomain[1] === yDomain[0] ? yDomain[1] + 1 : yDomain[1]],
    range: [height - MARGIN.bottom, MARGIN.top],
  });
  const tick = formatY ?? defaultTick(yIsMoney);

  return (
    <Group>
      {y.ticks(4).map((tickValue) => (
        <line
          key={`g${String(tickValue)}`}
          x1={MARGIN.left}
          x2={width - MARGIN.right}
          y1={y(tickValue)}
          y2={y(tickValue)}
          stroke={HAIRLINE}
        />
      ))}
      <AxisLeft
        scale={y}
        left={MARGIN.left}
        numTicks={4}
        tickFormat={(value) => tick(Number(value))}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 10,
          textAnchor: 'end',
          dx: -6,
          dy: 3,
          fontFamily: 'var(--font-mono)',
        })}
      />
      <AxisBottom
        scale={x}
        top={height - MARGIN.bottom}
        numTicks={5}
        tickFormat={(value) => new Date(Number(value)).toISOString().slice(0, 10)}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 10,
          textAnchor: 'middle',
          dy: 6,
          fontFamily: 'var(--font-mono)',
        })}
      />
      {series.map((entry, index) => (
        <LinePath
          key={entry.label}
          data={entry.points as SeriesPoint[]}
          x={(point) => x(Date.parse(point.x))}
          y={(point) => y(point.y)}
          stroke={lineColour(entry.is_policy, index)}
          strokeWidth={entry.is_policy ? 2 : 1.25}
          strokeDasharray={entry.is_policy ? undefined : '4 3'}
        />
      ))}
    </Group>
  );
}

/** The active policy is solid ink; baselines are dashed muted, distinguished by dash
 *  pattern as well as hue so the chart survives greyscale like the band ramp does. */
function lineColour(isPolicy: boolean, index: number): string {
  if (isPolicy) return INK;
  const dashes = ['5 3', '2 3', '8 3', '1 3'];
  void dashes;
  return index % 2 === 0 ? INK_MUTED : INK_FAINT;
}

function Legend({ series }: { series: readonly { label: string; is_policy: boolean }[] }): ReactElement {
  return (
    <ul style={{ display: 'flex', gap: 12, flexWrap: 'wrap', listStyle: 'none', margin: '4px 0 0', padding: 0 }}>
      {series.map((entry) => (
        <li key={entry.label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            aria-hidden="true"
            style={{
              display: 'inline-block',
              width: 18,
              height: 0,
              borderTop: `${entry.is_policy ? 2 : 1}px ${entry.is_policy ? 'solid' : 'dashed'} ${
                entry.is_policy ? INK : INK_MUTED
              }`,
            }}
          />
          <span
            style={{
              fontSize: 'var(--text-micro)',
              color: entry.is_policy ? 'var(--color-ink)' : 'var(--color-ink-muted)',
            }}
          >
            {entry.label}
          </span>
        </li>
      ))}
    </ul>
  );
}

/* --------------------------------------------------------------- bars ---- */

export type BarProps = {
  rows: readonly { label: string; value: number; secondary?: number; colour?: string }[];
  height?: number;
  valueLabel: string;
  secondaryLabel?: string;
  formatValue?: (value: number) => string;
  ariaLabel: string;
};

/** Paired bar-and-line: the scorecard's bin view, where population share is a bar and
 *  bad rate is a line on a second axis, and reading them together is the point. */
export function BarWithLine({
  rows,
  height = CHART_HEIGHT,
  valueLabel,
  secondaryLabel,
  formatValue,
  ariaLabel,
}: BarProps): ReactElement {
  const note = minimumSeriesNote(rows.length, 'bar');
  if (note !== null) {
    return (
      <div style={{ width: '100%', height }}>
        <ChartFrame height={height} note={note} label={ariaLabel} />
      </div>
    );
  }
  return (
    <div style={{ width: '100%', height }}>
      <ParentSize>
        {({ width, height: boxHeight }) => (
          <BarWithLineInner
            width={Math.max(width, 1)}
            height={boxHeight}
            rows={rows}
            valueLabel={valueLabel}
            secondaryLabel={secondaryLabel}
            formatValue={formatValue}
            ariaLabel={ariaLabel}
          />
        )}
      </ParentSize>
    </div>
  );
}

function BarWithLineInner({
  width,
  height,
  rows,
  secondaryLabel,
  formatValue,
  ariaLabel,
}: {
  width: number;
  height: number;
  rows: readonly { label: string; value: number; secondary?: number; colour?: string }[];
  valueLabel: string;
  secondaryLabel?: string;
  formatValue?: ((value: number) => string) | undefined;
  ariaLabel: string;
}): ReactElement {
  const [hover, setHover] = useState<string | null>(null);
  const x = scaleBand({
    domain: rows.map((row) => row.label),
    range: [MARGIN.left, width - MARGIN.right],
    padding: 0.28,
  });
  const y = bandLinear({
    domain: [0, Math.max(...rows.map((row) => row.value)) || 1],
    range: [height - MARGIN.bottom, MARGIN.top],
  });
  const hasSecondary = secondaryLabel !== undefined && rows.some((row) => row.secondary !== undefined);
  const y2 = bandLinear({
    domain: [0, Math.max(...rows.map((row) => row.secondary ?? 0)) || 1],
    range: [height - MARGIN.bottom, MARGIN.top],
  });
  const tick = formatValue ?? ((value: number) => String(Math.round(value)));

  const points = rows.filter((row) => row.secondary !== undefined);

  return (
    <svg
      width={width}
      height={height}
      role="img"
      aria-label={ariaLabel}
      data-chart="bar-line"
      data-points={rows.length}
      onMouseMove={(event) => {
        // Hover band, computed from the element's own box rather than a visx helper:
        // @visx/event is not in the pinned dependency set and an axis tooltip is not
        // worth adding one for.
        const box = event.currentTarget.getBoundingClientRect();
        const px = event.clientX - box.left;
        const found = rows.find((row) => {
          const left = x(row.label) ?? 0;
          return px >= left && px <= left + (x.bandwidth() || 0);
        });
        setHover(found?.label ?? null);
      }}
      onMouseLeave={() => setHover(null)}
    >
      <AxisLeft
        scale={y}
        left={MARGIN.left}
        numTicks={4}
        tickFormat={(value) => tick(Number(value))}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 10,
          textAnchor: 'end',
          dx: -6,
          dy: 3,
          fontFamily: 'var(--font-mono)',
        })}
      />
      {rows.map((row) => (
        <Bar
          key={row.label}
          x={x(row.label) ?? 0}
          y={y(Math.max(0, row.value))}
          width={x.bandwidth()}
          height={Math.max(0, y(0) - y(row.value))}
          fill={row.colour ?? 'var(--color-band-c)'}
          opacity={hover === null || hover === row.label ? 1 : 0.55}
        />
      ))}
      {hasSecondary ? (
        <LinePath
          data={points as { label: string; secondary?: number }[]}
          x={(point) => (x(point.label) ?? 0) + x.bandwidth() / 2}
          y={(point) => y2(point.secondary ?? 0)}
          stroke={EVIDENCE}
          strokeWidth={1.75}
        />
      ) : null}
      {hasSecondary ? (
        <AxisLeft
          scale={y2}
          left={width - MARGIN.right}
          numTicks={3}
          tickFormat={(value) => `${Math.round(Number(value) * 100)}%`}
          stroke={HAIRLINE}
          tickStroke={HAIRLINE}
          tickLabelProps={() => ({
            fill: EVIDENCE,
            fontSize: 10,
            textAnchor: 'start',
            dx: 6,
            dy: 3,
            fontFamily: 'var(--font-mono)',
          })}
        />
      ) : null}
      <AxisBottom
        scale={x}
        top={height - MARGIN.bottom}
        stroke={HAIRLINE}
        tickStroke={HAIRLINE}
        tickLabelProps={() => ({
          fill: INK_FAINT,
          fontSize: 9,
          textAnchor: 'end',
          dx: -4,
          dy: 4,
          fontFamily: 'var(--font-mono)',
        })}
      />
    </svg>
  );
}

/* ------------------------------------------------------------ waterfall -- */

export type WaterfallRow = {
  label: string;
  value: number;
  direction: 'increases' | 'decreases';
  /** Click target for the cross-filter; the row is a button, not just a bar. */
  onSelect?: () => void;
  selected?: boolean;
};

/** SHAP waterfall. Bars are drawn from the running total so the reader can add the
 *  column by eye, which is the same claim the scorecard points table makes. */
export function Waterfall({
  rows,
  ariaLabel,
  formatValue,
}: { rows: readonly WaterfallRow[]; ariaLabel: string; formatValue?: (value: number) => string }): ReactElement {
  const note = minimumSeriesNote(rows.length, 'waterfall');
  if (note !== null) return <ChartFrame height={120} note={note} label={ariaLabel} />;

  let running = 0;
  const segments = rows.map((row) => {
    const start = running;
    running += row.value;
    return { row, start, end: running };
  });
  const domain = [
    Math.min(0, ...segments.map((s) => Math.min(s.start, s.end))),
    Math.max(0, ...segments.map((s) => Math.max(s.start, s.end))),
  ] as const;
  const scale = bandLinear({ domain: [domain[0], domain[1] === domain[0] ? 1 : domain[1]], range: [0, 100] });
  const tick = formatValue ?? ((value: number) => value.toFixed(2));

  return (
    <ol
      data-chart="waterfall"
      data-points={rows.length}
      aria-label={ariaLabel}
      style={{ listStyle: 'none', margin: 0, padding: 0 }}
    >
      {segments.map((segment) => {
        const { row, start, end } = segment;
        const low = Math.min(start, end);
        const high = Math.max(start, end);
        return (
          <li
            key={row.label}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0,1fr) 84px 68px',
              alignItems: 'center',
              gap: 8,
              height: 'var(--spacing-row)',
              borderBottom: '1px solid var(--color-hairline)',
            }}
          >
            <button
              type="button"
              onClick={row.onSelect}
              disabled={row.onSelect === undefined}
              title={
                row.onSelect === undefined
                  ? row.label
                  : 'Cross-filter the evidence and transaction panes to this contribution'
              }
              style={{
                textAlign: 'left',
                background: row.selected ? 'var(--color-elev-2)' : 'transparent',
                border: 'none',
                color: row.selected ? EVIDENCE : 'var(--color-ink)',
                cursor: row.onSelect === undefined ? 'default' : 'pointer',
                fontSize: 'var(--text-label)',
                fontFamily: 'var(--font-sans)',
                padding: '0 4px',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {row.label}
            </button>
            <span aria-hidden="true" style={{ position: 'relative', height: 12, background: 'var(--color-hairline)' }}>
              <span
                style={{
                  position: 'absolute',
                  top: 0,
                  bottom: 0,
                  left: `${scale(low)}%`,
                  width: `${Math.max(scale(high) - scale(low), 0.5)}%`,
                  background: row.direction === 'decreases' ? 'var(--color-band-e)' : 'var(--color-band-b)',
                }}
              />
            </span>
            <span
              className="u-num"
              style={{
                textAlign: 'right',
                fontSize: 'var(--text-micro)',
                color: 'var(--color-ink-muted)',
                fontFamily: 'var(--font-mono)',
              }}
            >
              {row.value >= 0 ? `+${tick(row.value)}` : tick(row.value)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/* --------------------------------------------------------------- misc ---- */

export function extent(values: readonly number[]): [number, number] {
  if (values.length === 0) return [0, 1];
  return [Math.min(...values), Math.max(...values)];
}

function defaultTick(yIsMoney: boolean): (value: number) => string {
  if (!yIsMoney) return (value) => value.toFixed(2);
  return (value) => {
    const major = value / 100;
    const abs = Math.abs(major);
    if (abs >= 1_000_000_000) return `${(major / 1_000_000_000).toFixed(1)}bn`;
    if (abs >= 1_000_000) return `${(major / 1_000_000).toFixed(1)}m`;
    if (abs >= 1_000) return `${(major / 1_000).toFixed(0)}k`;
    return major.toFixed(0);
  };
}

/** THE minimum-series guard. One point is a fact, not a curve; two is a line and no
 *  more; zero is an absence that has to be named. Each case says what it is rather
 *  than drawing something that implies a trend the data cannot carry. */
export function minimumSeriesNote(count: number, kind: 'line' | 'bar' | 'waterfall' | 'grid'): string | null {
  if (count > 1) return null;
  if (count === 1) {
    return kind === 'bar'
      ? 'One bin was returned. A single bar is a count, not a distribution, so no axis is drawn — the value is printed in the table beside this panel.'
      : 'One point was returned for this series. A line through one point would draw a trend that the data does not contain, so the value is printed instead of plotted.';
  }
  return 'The response contained no points for this series. Nothing is drawn because nothing was sent; the table beside this panel shows what the route returned.';
}

/** Fixed-height frame so a guard note costs the same vertical space as the chart it
 *  replaced. A chart that renders shorter than its skeleton is a layout shift. */
export function ChartFrame({ height, note, label }: { height: number; note: string; label: string }): ReactElement {
  return (
    <div
      role="note"
      aria-label={label}
      data-chart-note
      style={{
        height,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '0 16px',
        textAlign: 'center',
        border: `1px dashed ${HAIRLINE}`,
        borderRadius: 'var(--radius-control)',
        background: CANVAS_RAISED,
        color: INK_MUTED,
        fontSize: 'var(--text-label)',
        fontFamily: 'var(--font-sans)',
      }}
    >
      {note}
    </div>
  );
}

export function ChartSlot({ children, height = CHART_HEIGHT }: { children: ReactNode; height?: number }): ReactElement {
  return <div style={{ width: '100%', height }}>{children}</div>;
}
