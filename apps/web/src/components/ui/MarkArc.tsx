/* =============================================================================
   The one licensed spinner.

   DESIGN.md §4: "the one licensed spinner is the OXBOW mark as a DETERMINATE arc,
   used only for user-triggered work with no known output shape — the CP-SAT solve."
   This file is the only place in the product that draws a spinning thing, and the
   type enforces why: `progress` is the fraction completed, and it is only ever
   omitted by the caller that genuinely has no denominator.

   When there is no denominator the component says so in words rather than inventing
   a sweeping arc, because an arc that sweeps at a made-up rate is the lying progress
   bar the SSE contract exists to prevent. A solve with no known shape gets a
   stationary arc and elapsed time, which is the same information a stage ledger
   gives and costs nothing to fake.

   Under reduced motion the rotation stops; the arc stays, because its length is the
   datum.
   ============================================================================= */

'use client';

import type { CSSProperties, ReactElement } from 'react';

import { usePrefersReducedMotion } from '@/design/motion';
import { T_MICRO } from './sx';

export type MarkArcProps = {
  /** 0–1 when the work has a denominator. Null when it does not, which is the case
   *  the component exists for, and which it states rather than disguises. */
  progress: number | null;
  label: string;
  size?: number;
  /** Elapsed milliseconds, from the caller's clock or the server's. */
  elapsedMs?: number | null;
  className?: string;
};

const ARC_RADIUS = 40;
const ARC_CIRCUMFERENCE = 2 * Math.PI * ARC_RADIUS;

export function MarkArc({ progress, label, size = 28, elapsedMs = null, className }: MarkArcProps): ReactElement {
  const reduced = usePrefersReducedMotion();
  const known = progress !== null && Number.isFinite(progress);
  const swept = known ? Math.min(Math.max(progress, 0), 1) : 0.25;

  return (
    <span
      className={className}
      role="status"
      aria-live="polite"
      data-mark-arc
      data-determinate={known}
      style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 100 100"
        aria-hidden="true"
        focusable="false"
        style={{
          transform: reduced ? 'none' : undefined,
          animation: reduced || !known ? undefined : 'oxbow-arc-rotate 1.4s linear infinite',
        }}
      >
        <style href="oxbow-arc" precedence="oxbow-motion">
          {ARC_KEYFRAMES}
        </style>
        <circle cx={50} cy={50} r={ARC_RADIUS} fill="none" stroke="var(--color-hairline-strong)" strokeWidth={8} />
        <circle
          cx={50}
          cy={50}
          r={ARC_RADIUS}
          fill="none"
          stroke="var(--color-evidence)"
          strokeWidth={8}
          strokeLinecap="butt"
          strokeDasharray={`${(ARC_CIRCUMFERENCE * swept).toFixed(1)} ${ARC_CIRCUMFERENCE.toFixed(1)}`}
          transform="rotate(-90 50 50)"
        />
      </svg>
      <span style={LABEL as CSSProperties}>
        {label}
        {known ? ` · ${(swept * 100).toFixed(0)}%` : ' · no known denominator'}
        {elapsedMs !== null ? ` · ${(elapsedMs / 1000).toFixed(1)}s` : ''}
      </span>
    </span>
  );
}

const LABEL = { ...T_MICRO, color: 'var(--color-ink-muted)' };

/** Rotation only, no scale, no opacity pulse: the arc's LENGTH is the progress, and a
 *  spinner that also breathes implies two things at once and measures neither. */
export const ARC_KEYFRAMES = `@keyframes oxbow-arc-rotate {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}`;
