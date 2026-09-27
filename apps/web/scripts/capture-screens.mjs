/**
 * Captures the submission's interface screenshots (and the stills the demo video is cut
 * from) into docs/screens/. Committed because "at least 3 high-quality images showing your
 * interface" is a required deliverable and a deliverable that can only be produced by
 * someone remembering the incantation is not a deliverable.
 *
 * Run with the app already serving in fixture mode:
 *   OXBOW_DATA_MODE=fixture node node_modules/next/dist/bin/next start --port 3100
 *   node scripts/capture-screens.mjs
 *
 * Fixture mode is deliberate: the live API's decoders still diverge from the served
 * payloads, so a live capture would photograph error panes.
 */
import { existsSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { chromium } from '@playwright/test';

const BASE = process.env.OXBOW_WEB_BASE_URL ?? 'http://127.0.0.1:3100';
const OUT = fileURLToPath(new URL('../../../docs/screens/', import.meta.url));
const CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2D';

const SHOTS = [
  ['01-dashboard', '/dashboard'],
  ['02-alerts-queue', '/alerts'],
  ['03-network-explorer', '/network?account=ACC-00DORM&hops=2'],
  ['04-scorecard', '/scorecard'],
  ['05-case-workspace', `/cases/${CASE_ID}`],
  ['06-policy-frontier', '/policy'],
  ['07-model-validation', '/model'],
  ['08-state-gallery', '/dev/states'],
];

/** Newest chromium in a Playwright browser cache. Same rule playwright.config.ts uses. */
function findCachedChromium() {
  const bases = [
    [process.env.LOCALAPPDATA, 'ms-playwright', 'chrome-win64/chrome.exe'],
    [process.env.XDG_CACHE_HOME, 'ms-playwright', 'chrome-linux/chrome'],
    [process.env.HOME, '.cache/ms-playwright', 'chrome-linux/chrome'],
    [process.env.HOME, 'Library/Caches/ms-playwright', 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'],
  ];
  for (const [base, sub, exe] of bases) {
    if (!base) continue;
    const root = `${base.replace(/\\/g, '/')}/${sub}`;
    if (!existsSync(root)) continue;
    const newest = readdirSync(root)
      .filter((n) => /^chromium-\d+$/.test(n))
      .sort()
      .at(-1);
    if (!newest) continue;
    const candidate = `${root}/${newest}/${exe}`;
    if (existsSync(candidate)) return candidate;
  }
  return undefined;
}

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.OXBOW_CHROME_PATH ?? findCachedChromium(),
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

for (const [name, route] of SHOTS) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle' });
  // The panes resolve on a timer in fixture mode; shooting the first frame would capture
  // the skeletons the CLS suite exists to prevent.
  await page.waitForTimeout(2_500);
  const path = `${OUT}${name}.png`;
  await page.screenshot({ path, fullPage: false });
  console.log(`${name}.png  ${route}`);
}

await browser.close();
