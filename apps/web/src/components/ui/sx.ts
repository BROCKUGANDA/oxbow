/* =============================================================================
   Shared inline-style fragments.

   WHY THIS EXISTS. `src/design/primitives/*` style themselves with inline objects
   that reference CSS custom properties, and the rule for app components is the
   same one: no new colour or spacing literal, ever. Repeating
   `color: 'var(--color-ink-muted)'` in sixty files is how a drift starts, so the
   vocabulary lives here once and a component composes it.

   Every value below resolves to a token declared in `src/design/tokens.css`. The
   one thing added here is *composition* — a panel is a hairline plus a radius plus
   a surface — never a new colour, size or duration.
   ============================================================================= */

import type { CSSProperties } from 'react';

/** Panels: 6px radius, hairline border, raised canvas, zero blur. */
export const PANEL: CSSProperties = {
  border: '1px solid var(--color-hairline)',
  borderRadius: 'var(--radius-panel)',
  background: 'var(--color-canvas-raised)',
};

/** The inset variant: a panel inside a panel sits on the sunken surface. */
export const PANEL_SUNKEN: CSSProperties = {
  border: '1px solid var(--color-hairline)',
  borderRadius: 'var(--radius-control)',
  background: 'var(--color-canvas-sunken)',
};

export const STACK: CSSProperties = { display: 'flex', flexDirection: 'column' };
export const ROW: CSSProperties = { display: 'flex', alignItems: 'center' };
export const ROW_BASELINE: CSSProperties = { display: 'flex', alignItems: 'baseline' };
export const WRAP: CSSProperties = { flexWrap: 'wrap' };

/** The gap vocabulary. Two values, from --spacing-pane-gap and half of it. */
export const GAP: CSSProperties = { gap: 'var(--spacing-pane-gap)' };
export const GAP_TIGHT: CSSProperties = { gap: 'calc(var(--spacing-pane-gap) / 2)' };

export const SECTION_PAD: CSSProperties = { padding: 'var(--spacing-pane-gap)' };

/** Type roles, from the --text-* scale in tokens.css. */
export const T_HERO: CSSProperties = {
  fontFamily: 'var(--font-condensed)',
  fontSize: 'var(--text-hero)',
  lineHeight: 1,
  letterSpacing: '-0.02em',
  fontWeight: 600,
  fontVariantNumeric: 'tabular-nums',
  margin: 0,
};

export const T_KPI: CSSProperties = {
  fontFamily: 'var(--font-condensed)',
  fontSize: 'var(--text-kpi)',
  lineHeight: 1.1,
  letterSpacing: '-0.01em',
  fontWeight: 600,
  fontVariantNumeric: 'tabular-nums',
  margin: 0,
};

export const T_BODY: CSSProperties = {
  fontFamily: 'var(--font-sans)',
  fontSize: 'var(--text-body)',
  lineHeight: 1.5,
  color: 'var(--color-ink)',
  margin: 0,
};

export const T_LABEL: CSSProperties = {
  fontFamily: 'var(--font-sans)',
  fontSize: 'var(--text-label)',
  lineHeight: 1.33,
  fontWeight: 500,
  color: 'var(--color-ink-muted)',
  margin: 0,
};

export const T_MICRO: CSSProperties = {
  fontFamily: 'var(--font-sans)',
  fontSize: 'var(--text-micro)',
  lineHeight: 1.45,
  fontWeight: 500,
  color: 'var(--color-ink-faint)',
  margin: 0,
};

/** Identifiers and evidence strings are mono, everywhere, without exception. */
export const T_MONO: CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontVariantNumeric: 'tabular-nums',
  margin: 0,
};

/** Table and queue row geometry. Skeletons are built from these same two values,
 *  which is the only reason matched-geometry zero-CLS is achievable at all. */
export const ROW_HEIGHT = 'var(--spacing-row)';
export const TABLE_ROW_HEIGHT = 'var(--spacing-table-row)';

export const HAIRLINE_BOTTOM: CSSProperties = { borderBottom: '1px solid var(--color-hairline)' };
export const HAIRLINE_RIGHT: CSSProperties = { borderRight: '1px solid var(--color-hairline)' };
export const HAIRLINE_TOP: CSSProperties = { borderTop: '1px solid var(--color-hairline)' };

/** Controls: 3px radius, hairline-strong border, 140ms hover. */
export const CONTROL: CSSProperties = {
  fontFamily: 'var(--font-sans)',
  fontSize: 'var(--text-label)',
  fontWeight: 500,
  color: 'var(--color-ink)',
  background: 'transparent',
  border: '1px solid var(--color-hairline-strong)',
  borderRadius: 'var(--radius-control)',
  padding: '4px 10px',
  cursor: 'pointer',
  transition: `background var(--duration-fast) var(--ease-out-quint), border-color var(--duration-fast) var(--ease-out-quint)`,
};

export const CONTROL_PRIMARY: CSSProperties = {
  ...CONTROL,
  color: 'var(--color-ink-inverse)',
  background: 'var(--color-ink)',
  border: '1px solid var(--color-ink)',
};

export const CONTROL_ON: CSSProperties = {
  ...CONTROL,
  color: 'var(--color-canvas)',
  background: 'var(--color-evidence)',
  border: '1px solid var(--color-evidence)',
};

/** The focus ring. One ring in the product; it is an outline, not a shadow. */
export const FOCUS_OUTLINE = '2px solid var(--color-focus)';

/** Truncation with the full value kept reachable — see `test_long_text_truncated_and_escaped`. */
export const ELLIPSIS: CSSProperties = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  minWidth: 0,
};

/** Grid helper: N equal columns from a token-derived minimum. */
export function autoGrid(min: string): CSSProperties {
  return { display: 'grid', gridTemplateColumns: `repeat(auto-fit, minmax(${min}, 1fr))` };
}
