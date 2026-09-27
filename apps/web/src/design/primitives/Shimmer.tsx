/* =============================================================================
   OXBOW Shimmer.
   Spec 12.6 state craft, 12.7 MOTION.

   The sweep is transform: translateX() on the compositor. It is NEVER an animated
   background-position, which repaints every frame and turns a loading state into
   a scroll-performance problem on the exact machine that is already struggling.

   1.4s loop, will-change: transform, and paused under reduced motion. The pause
   is not a fade to a static gradient: a moving highlight is the signal that work
   is in flight, so a reduced-motion skeleton shows the same shape at rest.
   ============================================================================= */

import type { CSSProperties, ReactElement } from 'react';

import { usePrefersReducedMotion } from '../motion';

export interface ShimmerProps {
  /** The block's own geometry. Width and height are required, because a
   *  shimmer with no box has nothing to sweep. */
  width: number | string;
  height: number | string;
  /** Border radius. Defaults to the control radius. */
  radius?: string;
  className?: string;
  style?: CSSProperties;
  /** Set false to render a plain placeholder block with no sweep at all. */
  active?: boolean;
  /** Accessible description, or the shimmer is an unannounced mystery. */
  label?: string;
}

/** One period of the sweep, in seconds. */
const SHIMMER_PERIOD_S = 1.4;

export function Shimmer({
  width,
  height,
  radius = 'var(--radius-control)',
  className,
  style,
  active = true,
  label,
}: ShimmerProps): ReactElement {
  const reducedMotion = usePrefersReducedMotion();
  const sweeping = active && !reducedMotion;

  return (
    <>
      {/* Hoisted and de-duplicated by React 19 (href + precedence), so the
          keyframes exist exactly once no matter how many shimmers mount. */}
      <style href="oxbow-shimmer" precedence="oxbow-motion">
        {SHIMMER_KEYFRAMES}
      </style>
      <div
        className={className}
        style={{ position: 'relative', overflow: 'hidden', width, height, borderRadius: radius, ...style }}
        role={label === undefined ? undefined : 'status'}
        aria-label={label}
        aria-hidden={label === undefined ? true : undefined}
      >
        {/* The resting block. Always painted, so the shape is right even when the
            sweep is paused and even before the first animation frame lands. */}
        <div
          style={{
            position: 'absolute',
            inset: 0,
            background: 'var(--color-elev-1)',
          }}
        />
        {sweeping ? <Sweep /> : null}
      </div>
    </>
  );
}

/**
 * The highlight. One element, transform only.
 *
 * The gradient is a fixed 40%-wide band translated from -150% to 250%: the
 * overshoot on both ends means the band is fully clear of the block at the loop
 * seam, so there is no visible jump when the animation restarts.
 */
function Sweep(): ReactElement {
  return (
    <div
      data-shimmer-sweep
      style={{
        position: 'absolute',
        inset: 0,
        width: '40%',
        background: 'linear-gradient(90deg, transparent 0%, oklch(1 0 0 / 0.055) 50%, transparent 100%)',
        willChange: 'transform',
        animation: `oxbow-shimmer ${SHIMMER_PERIOD_S}s linear infinite`,
      }}
    />
  );
}

/**
 * The keyframes, emitted once as a hoisted stylesheet.
 *
 * Declared here rather than in tokens.css because the animation is owned by this
 * component, and a stylesheet rule that only one component uses does not belong
 * in the global token file.
 */
export const SHIMMER_KEYFRAMES = `@keyframes oxbow-shimmer {
  0%   { transform: translate3d(-150%, 0, 0); }
  100% { transform: translate3d(250%, 0, 0); }
}`;

export default Shimmer;
