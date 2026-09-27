/**
 * Vitest setup for the OXBOW web app. jsdom does not ship these, and the
 * components reach for them through documented guards (`typeof window.matchMedia`
 * is checked in design/motion.ts; @visx/responsive constructs a ResizeObserver).
 * Stubs here are environment-level, never per-component: no component fetches
 * mocked data, and nothing in src/ branches on a test mode.
 */

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

type MqlListener = (event: { matches: boolean }) => void;

const mediaState = {
  reducedMotion: false,
  listeners: new Set<MqlListener>(),
};

window.matchMedia = (query: string): MediaQueryList => {
  const matches = query.includes('prefers-reduced-motion') ? mediaState.reducedMotion : false;
  return {
    matches,
    media: query,
    onchange: null,
    addEventListener: (_: string, listener: MqlListener) => mediaState.listeners.add(listener),
    removeEventListener: (_: string, listener: MqlListener) => mediaState.listeners.delete(listener),
    addListener: (listener: MqlListener) => mediaState.listeners.add(listener),
    removeListener: (listener: MqlListener) => mediaState.listeners.delete(listener),
    dispatchEvent: () => false,
  } as unknown as MediaQueryList;
};

/** The only way a test can change the reported motion preference; the hook
 *  subscribes, so notifying listeners is part of the contract. */
export function setPrefersReducedMotion(matches: boolean): void {
  mediaState.reducedMotion = matches;
  for (const listener of mediaState.listeners) listener({ matches });
}

class NoopResizeObserver {
  observe(): void {
    /* jsdom has no layout; charts measure 0 and the minimum-series guard is what
       the test asserts, not the drawn pixels. */
  }
  unobserve(): void {
    /* intentionally empty */
  }
  disconnect(): void {
    /* intentionally empty */
  }
}

globalThis.ResizeObserver = NoopResizeObserver as unknown as typeof ResizeObserver;
