import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * Vitest for the §14 UI edge cases that are testable without a browser:
 * the stale-response guard, the five empty states, the four error tiers,
 * matched-geometry skeletons, money-with-assumptions, timestamps with zone,
 * the minimum-series guard, reduced-motion pausing the shimmer.
 *
 * Browser-owned claims (screenshots of /dev/states, measured CLS, axe, 60fps,
 * real prefers-reduced-motion emulation) live in the Playwright suite —
 * `playwright.config.ts` + `tests/states/`, not here.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
    include: ['tests/unit/**/*.test.{ts,tsx}'],
    // jsdom leaks a ResizeObserver-free window; every test must be isolated.
    restoreMocks: true,
    unstubGlobals: true,
  },
});
