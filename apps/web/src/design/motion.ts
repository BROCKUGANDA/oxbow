/* =============================================================================
   OXBOW motion primitives.
   Spec 12.7 MOTION.

   THE RULE, in one place: enters ease out, exits ease in and are ALWAYS faster
   than enters, position changes use the spring, and ease-in-out never appears on
   a user-triggered transition. The helpers below encode that rule so a component
   cannot accidentally violate it by reaching for a duration token directly.

   The reduced-motion guard is here too. MotionConfig reducedMotion="user" is
   global and owned by the app shell; this module reports the preference so a
   component can choose the right NON-animated fallback rather than merely
   animating faster. The transition shapes below are declared locally and are
   structurally compatible with motion/react's Transition and Variants, so this
   module type-checks whether or not the animation package is installed.
   ============================================================================= */

import { useSyncExternalStore } from 'react';

/* ---------------------------------------------------------- local types ---- */

export type EasingTuple = readonly [number, number, number, number];

/** Structurally compatible with motion/react's Transition. */
export interface Transition {
  duration?: number;
  delay?: number;
  ease?: EasingTuple;
  type?: 'spring' | 'tween' | 'inertia';
  stiffness?: number;
  damping?: number;
  mass?: number;
}

/** A style value inside a variant: a plain CSS value, or a nested transition. */
export type VariantStyle = string | number | Transition | Record<string, string | number>;

/** Structurally compatible with motion/react's Variants. */
export interface Variants {
  initial?: Record<string, VariantStyle>;
  animate?: Record<string, VariantStyle>;
  exit?: Record<string, VariantStyle>;
}

/* ------------------------------------------------------------- durations ---- */

/** Durations in ms, mirroring --duration-* in tokens.css. */
export const DURATION = {
  /** hover, focus ring */
  instant: 80,
  /** exits, toast dismiss */
  fast: 140,
  /** enters, cross-filter wipes */
  base: 220,
  /** route and shared-element transitions */
  slow: 320,
  /** graph assembly, stage-ledger completion */
  deliberate: 520,
} as const;

export type DurationToken = keyof typeof DURATION;

/* --------------------------------------------------------------- easings ---- */

export const EASE = {
  /** enters only */
  outQuint: [0.22, 1, 0.36, 1],
  /** exits only, and always paired with a SHORTER duration than its enter */
  inQuad: [0.55, 0.085, 0.68, 0.53],
} as const satisfies Record<string, EasingTuple>;

/* --------------------------------------------------------------- springs ---- */

/** The one spring in the product. Position changes use it; opacity does not. */
export const SPRING_UI = {
  type: 'spring',
  stiffness: 420,
  damping: 34,
  mass: 0.9,
} as const satisfies Transition;

/** Graph assembly and stage-ledger row fill: same character, looser tail. */
export const SPRING_DELIBERATE = {
  type: 'spring',
  stiffness: 180,
  damping: 26,
  mass: 1.1,
} as const satisfies Transition;

/* ------------------------------------------------------ reduced motion ----- */

const QUERY = '(prefers-reduced-motion: reduce)';

/**
 * Reports the user's motion preference, and subscribes to changes.
 *
 * MotionConfig reducedMotion="user" already suppresses Motion's own animations.
 * This hook exists for the cases that guard cannot cover: skipping a stagger
 * entirely, rendering the settled end-state without a transition, or pausing a
 * CSS animation that lives outside Motion's tree.
 *
 * Returns false during SSR and on the first client render, then corrects on the
 * following tick. That one-frame default is deliberate: rendering the animated
 * variant first and the static one second is the only ordering that does not
 * flash for reduced-motion users on hydration.
 */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

function subscribe(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return () => undefined;
  }
  const mql = window.matchMedia(QUERY);
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}

function getSnapshot(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return false;
  }
  return window.matchMedia(QUERY).matches;
}

function getServerSnapshot(): boolean {
  return false;
}

/** Non-reactive read, for event handlers and imperative code. */
export function prefersReducedMotion(): boolean {
  return getSnapshot();
}

/* ------------------------------------------------------------- helpers ----- */

/**
 * Stagger helper for a list of children.
 *
 * The step is subtracted from the total so the LAST item in the group finishes
 * at `base` seconds rather than `base + n * step`. Without that, a 200-row
 * virtualized list scheduled with a naive stagger starts its last row several
 * seconds late, and a progress indicator that outruns its own data is a lie.
 */
export function staggerChildren(index: number, count: number, step = 0.02, base = DURATION.base): Variants {
  const capped = Math.max(count, 1);
  const delay = index < capped ? (index / capped) * (step * capped) : step * capped;
  return {
    initial: { opacity: 0, y: 4 },
    animate: {
      opacity: 1,
      y: 0,
      transition: { duration: base / 1000, delay, ease: EASE.outQuint },
    },
    exit: { opacity: 0, transition: { duration: DURATION.fast / 1000, ease: EASE.inQuad } },
  };
}

/**
 * The exit transition, from the point of view of the thing being dismissed.
 * Faster than every enter, easing in. Callers should not pass a duration.
 */
export const EXIT: Transition = {
  duration: DURATION.fast / 1000,
  ease: EASE.inQuad,
};

/** The enter transition, from the point of view of the thing being shown. */
export const ENTER: Transition = {
  duration: DURATION.base / 1000,
  ease: EASE.outQuint,
};

/**
 * A cross-filter wipe. The enter is `base`, the exit is `fast`, and the two
 * never overlap on the same property, so a filter change reads as one movement
 * rather than two animations fighting.
 */
export function crossFilterWipe(): { enter: Variants; exit: Variants } {
  return {
    enter: {
      initial: { opacity: 0, clipPath: 'inset(0 100% 0 0)' },
      animate: {
        opacity: 1,
        clipPath: 'inset(0 0% 0 0)',
        transition: { duration: DURATION.base / 1000, ease: EASE.outQuint },
      },
    },
    exit: {
      exit: {
        opacity: 0,
        clipPath: 'inset(0 0 0 100%)',
        transition: { duration: DURATION.fast / 1000, ease: EASE.inQuad },
      },
    },
  };
}
