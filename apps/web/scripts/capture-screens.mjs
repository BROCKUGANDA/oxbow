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

/**
 * The case and the account the submission screenshots open.
 *
 * These used to be literals — `01J4Z7M2QK9N7V1C4X6E8G0B2D` and `ACC-00DORM` — which are
 * fixture identifiers. Against a live warehouse they resolve to nothing, so the capture
 * "succeeded" and produced a page of skeletons and a not-found: eleven committed images of
 * the state gallery later, the screenshot component of the submission described the double
 * rather than the product. That is the §18 rule in image form: a screen showing a value no
 * response carries is not a screenshot, it is a mock-up.
 *
 * So: in fixture mode the bundled identifiers are honest, because the whole app is the
 * fixture and the caption says so. In any other mode the caller must name a case and an
 * account that a landed run actually holds, and this script refuses to guess.
 */
const FIXTURE_MODE = process.env.OXBOW_DATA_MODE === 'fixture';
const CASE_ID = process.env.OXBOW_DEMO_CASE_ID ?? (FIXTURE_MODE ? '01J4Z7M2QK9N7V1C4X6E8G0B2D' : undefined);
const ACCOUNT_KEY = process.env.OXBOW_DEMO_ACCOUNT_KEY ?? (FIXTURE_MODE ? 'ACC-00DORM' : undefined);

if (CASE_ID === undefined || ACCOUNT_KEY === undefined) {
  console.error(
    `capture-screens: OXBOW_DEMO_CASE_ID and OXBOW_DEMO_ACCOUNT_KEY are required unless OXBOW_DATA_MODE=fixture.\nTake them from the landed warehouse, e.g. the top-ranked alert and the case opened on it:\n  curl -s -H "Authorization: Bearer $TOKEN" '${BASE.replace(':3100', ':8123')}/api/alerts?limit=1'\nThen POST /api/cases for that account and pass the returned case_id here. Refusing rather than falling back to the fixture identifiers, because a screenshot of a fixture is evidence about the fixture.`,
  );
  process.exit(1);
}

const SHOTS = [
  ['01-dashboard', '/dashboard'],
  ['02-alerts-queue', '/alerts'],
  ['03-network-explorer', `/network?account=${ACCOUNT_KEY}&hops=2`],
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

/**
 * A screenshot is evidence only if the screen rendered evidence.
 *
 * The previous run of this script produced eight honest files that showed skeletons and one
 * pane correctly reporting that a table does not exist in the read-only warehouse — the app
 * behaving exactly as designed against an empty database, and a submission component built on
 * top of it. Nothing complained, because taking a picture cannot fail.
 *
 * So each route is asserted, not photographed-and-hoped: no contract refusal, no problem+json
 * body on screen, and at least one resolved content marker. A route that fails is named and the
 * run exits non-zero, because a red capture is a finding and a silent image is a lie.
 */
async function assertRendered(name, page) {
  const body = await page.locator('body').innerText();
  const offenders = [
    ['contract refusal', /Response did not match the API contract|expected .*, got undefined/],
    ['problem body on screen', /application\/problem\+json|Dependency unavailable|no priced accounts/],
    ['not found', /No case\b|Case not found|does not exist in the null-file warehouse/],
  ].filter(([, re]) => re.test(body));
  if (offenders.length > 0) {
    return `${name}: rendered ${offenders.map(([label]) => label).join(', ')}`;
  }
  // Something must have resolved: a figure, a run id, a row, the canvas, or a named empty
  // state. /dev/states deliberately enumerates every failure tier, so only the refusal checks
  // apply there.
  if (name === '08-state-gallery') return null;
  const marks = await page
    .locator('[data-money-figure], [data-run-id], table tbody tr, [data-cytoscape-host], [data-empty-state]')
    .count();
  if (marks === 0) return `${name}: no resolved content marker on the page`;
  return null;
}

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.OXBOW_CHROME_PATH ?? findCachedChromium(),
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

const failures = [];
for (const [name, route] of SHOTS) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle' });
  // The panes resolve on a timer in fixture mode; shooting the first frame would capture
  // the skeletons the CLS suite exists to prevent.
  await page.waitForTimeout(2_500);
  const failure = await assertRendered(name, page);
  if (failure !== null) {
    failures.push(failure);
    console.error(`FAIL  ${failure}  (${route})`);
    continue;
  }
  const path = `${OUT}${name}.png`;
  await page.screenshot({ path, fullPage: false });
  console.log(`${name}.png  ${route}`);
}

await browser.close();

if (failures.length > 0) {
  console.error(
    `\n${failures.length} of ${SHOTS.length} routes did not render evidence. The routes that passed were written; the rest were not. Fix the warehouse or the route and re-run — do not submit a screenshot of a skeleton.`,
  );
  process.exit(1);
}
