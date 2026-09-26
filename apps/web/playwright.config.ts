import { defineConfig } from '@playwright/test';

/**
 * Playwright for the §14 P8 gate: the screenshot-state suite over /dev/states, the
 * measured-CLS runs, real prefers-reduced-motion emulation, and the axe sweep.
 *
 * This host has no package manager and no downloaded Playwright browsers, and browsers
 * may not be installed; the config therefore launches the Chromium that is ALREADY on
 * the machine (the ms-playwright cache) via `executablePath`. Override with
 * OXBOW_CHROME_PATH. The web app and API are started externally (no `make`/`pnpm`
 * here): `next start` on 3100 with the /api proxy to the uvicorn on 8111.
 */
const CACHED_CHROMIUM = 'C:/Users/HP/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';

export default defineConfig({
  testDir: './tests/states',
  outputDir: '.test-output/playwright',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.OXBOW_WEB_BASE_URL ?? 'http://127.0.0.1:3100',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    launchOptions: {
      executablePath: process.env.OXBOW_CHROME_PATH ?? CACHED_CHROMIUM,
    },
  },
  projects: [{ name: 'chromium' }],
});
