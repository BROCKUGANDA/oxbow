/* =============================================================================
   OXBOW Skeleton.
   Spec 12.6 state craft.

   A skeleton is a promise about the resolved layout, and this one keeps the
   promise exactly. The same row heights, the same column widths, the same row
   count as the page that will replace it, so resolution produces ZERO layout
   shift. A skeleton that guesses wrong is worse than no skeleton, because the
   user watches the page jump twice.

   Every measurement here is a token, not a magic number, so a density change
   moves the skeleton and the real table together or not at all.
   ============================================================================= */

import type { CSSProperties, ReactElement } from 'react';

import { Shimmer } from './Shimmer';

export interface SkeletonColumn {
  /** Stable key; also the aria-label source. */
  key: string;
  /** Any CSS length. The real table's resolved width is the only correct answer. */
  width: string;
  /** Right-aligned columns are numeric and must be tabular. */
  align?: 'start' | 'end';
}

export interface SkeletonProps {
  /** Column geometry, in order. Must match the resolved table. */
  columns: readonly SkeletonColumn[];
  /** How many rows to draw. Match the page size the query will return. */
  rows?: number;
  /** Row box height in px. --spacing-table-row by default. */
  rowHeight?: number;
  /** Draw the header row. Off for card grids and KPI strips. */
  showHeader?: boolean;
  headerHeight?: number;
  /** Accessible description, or the skeleton is an unannounced mystery. */
  label?: string;
  className?: string;
  style?: CSSProperties;
}

const DEFAULT_ROW_HEIGHT = 36;
const DEFAULT_HEADER_HEIGHT = 32;

export function Skeleton({
  columns,
  rows = 8,
  rowHeight = DEFAULT_ROW_HEIGHT,
  showHeader = true,
  headerHeight = DEFAULT_HEADER_HEIGHT,
  label = 'Loading',
  className,
  style,
}: SkeletonProps): ReactElement {
  const rowCount = Math.max(rows, 0);

  return (
    <div
      className={className}
      style={{ display: 'flex', flexDirection: 'column', ...style }}
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-label={label}
    >
      <span className="u-sr-only">{label}</span>

      {showHeader ? (
        <div
          aria-hidden="true"
          style={{
            display: 'grid',
            gridTemplateColumns: columns.map((c) => c.width).join(' '),
            alignItems: 'center',
            height: headerHeight,
            padding: '0 12px',
            borderBottom: '1px solid var(--color-hairline)',
          }}
        >
          {columns.map((col) => (
            <SkeletonCell key={col.key} width={col.width} align={col.align} height={10} />
          ))}
        </div>
      ) : null}

      {/* Index keys are correct here and deliberately so. These rows are
          indistinguishable placeholders with no identity of their own: they hold
          no state, carry no focus, and are replaced wholesale by the resolved
          table. A stable id per row would only make React believe row 3 has a
          persistent identity it does not have, and would remount the shimmer on
          every page-size change. */}
      {Array.from({ length: rowCount }, (_, rowIndex) => (
        <div
          key={rowIndex}
          aria-hidden="true"
          style={{
            display: 'grid',
            gridTemplateColumns: columns.map((c) => c.width).join(' '),
            alignItems: 'center',
            height: rowHeight,
            padding: '0 12px',
            /* Row separators are hairlines, never shadow, and never a rounded card. */
            borderBottom: '1px solid var(--color-hairline)',
          }}
        >
          {columns.map((col) => (
            <SkeletonCell key={col.key} width={col.width} align={col.align} height={12} />
          ))}
        </div>
      ))}
    </div>
  );
}

interface SkeletonCellProps {
  width: string;
  align?: 'start' | 'end';
  height: number;
}

function SkeletonCell({ width, align, height }: SkeletonCellProps): ReactElement {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: align === 'end' ? 'flex-end' : 'flex-start',
        padding: '0 8px',
      }}
    >
      <Shimmer
        width={width}
        height={height}
        /* 3px on a table cell reads as a control, not a block. Cells are 0px. */
        radius="var(--radius-control)"
      />
    </div>
  );
}

/* Screen-reader-only text, without pulling in a utility framework. */
export const SR_ONLY_CLASS = 'u-sr-only';
