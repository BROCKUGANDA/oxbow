/**
 * §14 P8 gate, second clause: axe reports zero critical violations, on every route,
 * run from the axe-core already vendored in node_modules — injected into the live
 * page, not a mocked audit.
 */
import { expect, test, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const ROUTES = [
  '/',
  '/dashboard',
  '/alerts',
  '/cases/01J4Z7M2QK9N7V1C4X6E8G0B2D',
  '/network?account=ACC-00DORM',
  '/scorecard',
  '/policy',
  '/model',
  '/dev/states',
  '/dev/states?motion=reduced',
];

function axeMinPath(): string {
  const candidates = [
    path.join(process.cwd(), 'node_modules/axe-core/axe.min.js'),
    path.join(process.cwd(), 'node_modules/.pnpm/axe-core@4.10.3/node_modules/axe-core/axe.min.js'),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(`axe.min.js not found; looked at ${candidates.join(', ')}`);
}

type AxeNode = { impact?: string; target: string[] };
type AxeViolation = { id: string; impact: string | null; nodes: AxeNode[] };
type AxeResult = { violations: AxeViolation[] };

async function runAxe(page: Page): Promise<AxeResult> {
  await page.addScriptTag({ path: axeMinPath() });
  return page.evaluate(async () => {
    // axe accepts a root Element or the Document; narrowing the injected global to one
    // or the other is what `Axe.run` actually is, so the whole tree is scanned.
    const axe = (
      window as unknown as {
        axe: { run: (el: Element | Document, opts: Record<string, unknown>) => Promise<AxeResult> };
      }
    ).axe;
    return axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] });
  });
}

test.describe('axe on every route', () => {
  for (const route of ROUTES) {
    test(`zero critical and zero serious violations on ${route}`, async ({ page }) => {
      await page.goto(route, { waitUntil: 'load' });
      await page.waitForTimeout(3_000);
      const result = await runAxe(page);
      const critical = result.violations.filter((v) => v.impact === 'critical');
      const serious = result.violations.filter((v) => v.impact === 'serious');
      const summary = result.violations.map((v) => `${String(v.impact)}:${v.id}×${String(v.nodes.length)}`);
      console.log(`axe ${route}: ${summary.length === 0 ? 'clean' : summary.join(' · ')}`);
      expect(critical).toEqual([]);
      expect(serious).toEqual([]);
    });
  }
});
