import fs from 'node:fs';
import path from 'node:path';
/**
 * §14 P8 gate, first clause: the screenshot suite over /dev/states.
 *
 * Each section is screenshotted AND asserted semantically — a state that renders but
 * says the wrong thing is exactly the failure a pixel-diff suite cannot see. Screens
 * land under tests/states/.screens/ and are kept as the record of what was rendered;
 * the suite is screenshot-green by construction, not snapshot-brittle.
 */
import { type Page, expect, test } from '@playwright/test';

const SCREENS = path.join(__dirname, '.screens');

async function shoot(page: Page, name: string, target?: string): Promise<void> {
  fs.mkdirSync(SCREENS, { recursive: true });
  const file = path.join(SCREENS, `${name}.png`);
  if (target === undefined) {
    await page.screenshot({ path: file, fullPage: true });
  } else {
    await page.locator(target).screenshot({ path: file });
  }
}

const SECTIONS = ['bands', 'glyphs', 'loading', 'ledger', 'empty', 'error', 'degraded', 'money', 'edges', 'type'];

test.describe('/dev/states — the whole gallery', () => {
  test('renders every named section, screenshotted', async ({ page }) => {
    await page.goto('/dev/states', { waitUntil: 'load' });
    await expect(page.getByRole('heading', { name: 'State gallery' })).toBeVisible();
    for (const id of SECTIONS) {
      await expect(page.locator(`section[data-gallery-section="${id}"]`)).toBeVisible();
      await shoot(page, `section-${id}`, `section[data-gallery-section="${id}"]`);
    }
    await shoot(page, 'full-gallery');
  });

  test('five distinct empty states, none of which says "No data"', async ({ page }) => {
    await page.goto('/dev/states', { waitUntil: 'load' });
    const empty = page.locator('section[data-gallery-section="empty"]');
    await expect(empty.getByRole('heading').first()).toBeVisible();
    // Asserted against the section's own text. Every figure inside these sentences is a
    // tabular span, so `getByText` on a whole sentence fails at an element boundary
    // rather than on the copy the page actually shows -- which is a selector problem,
    // not a missing state, and this suite has to tell the two apart.
    const body = await empty.innerText();
    for (const phrase of [
      /Your filters exclude all 1,412 alerts/,
      /No run recorded yet/,
      /ACC-00DORM has no counterparties between/,
      /The two models agree on every account above band C/,
      /No cycle survived the time-respecting filter/,
    ]) {
      expect(body, `the empty section does not show ${String(phrase)}`).toMatch(phrase);
    }
    // The section's own caption is `Five empty states, none of which says "no data"` --
    // it names the rule, so it must be excluded before the rule is enforced on the arms.
    // Compared lower-cased, because the caption renders title-case and the assertion below
    // folds the whole section: a case-sensitive exclusion lets the caption back in as a
    // failure of the rule it is describing.
    const arms = body
      .split('\n')
      .filter((line) => !line.toLowerCase().startsWith('five empty states'))
      .join('\n');
    expect(arms.toLowerCase()).not.toContain('no data');
    // §14 copy obligations: narrowest predicate + the count that would return, and
    // the literal pipeline command with a copy button.
    expect(body).toMatch(/Removing just that one returns 41 alerts/);
    expect(body).toContain('uv run oxbow pipeline');
    await expect(empty.getByRole('button', { name: 'Copy' })).toBeVisible();
  });

  test('every error surface that has a run_id prints it with a copy button', async ({ page }) => {
    await page.goto('/dev/states', { waitUntil: 'load' });
    const error = page.locator('section[data-gallery-section="error"]');
    // the pane-level ErrorPanes; the inline-field tier is a plain role=alert <p>.
    const alerts = error.locator('[role="alert"][data-pane]');
    await expect(alerts).not.toHaveCount(0);
    for (const surface of await alerts.all()) {
      const runId = surface.locator('code');
      if ((await runId.count()) > 0) {
        await expect(runId.first()).toContainText('01J4Z7M2QK9N7V1C4X6E8G0B2D');
        await expect(surface.getByRole('button', { name: 'Copy' })).toBeVisible();
      } else {
        await expect(surface.getByText('No run_id')).toBeVisible();
      }
    }
  });

  test('the gallery carries the four tiers, the degraded banner and the matched skeletons', async ({ page }) => {
    await page.goto('/dev/states', { waitUntil: 'load' });
    await expect(page.getByText('tier 1 · inline field')).toBeVisible();
    await expect(page.getByText(/tier 2 · pane-level, retrying in place/)).toBeVisible();
    await expect(page.getByText(/tier 3 · route-level error.tsx/)).toBeVisible();
    await expect(page.getByText(/tier 4 · global-error.tsx/)).toBeVisible();
    await expect(page.getByText(/Degraded: CP-SAT solver port unavailable/)).toBeVisible();
    // a currency figure with no assumptions must REFUSE to render, visibly.
    await expect(page.getByText(/has no assumption line/)).toBeVisible();
    // the minimum-series guard prints its note in the edges section. Addressed by its
    // accessible name rather than by text: the note is a div[role=note] wrapping a <p>, so a
    // bare getByText resolves to both and strict mode fails on the duplication, not on the copy.
    await expect(page.getByRole('note', { name: 'Single point series' })).toContainText(
      'One point was returned for this series',
    );
  });

  test('the disclaimer is in the footer of every route, verbatim', async ({ page }) => {
    for (const route of ['/', '/dashboard', '/alerts', '/network', '/scorecard', '/policy', '/model', '/dev/states']) {
      await page.goto(route, { waitUntil: 'domcontentloaded' });
      const footer = page.locator('footer [data-disclaimer]');
      await expect(footer).toBeVisible();
      await expect(footer).toContainText('OXBOW is a research prototype');
      await expect(footer).toContainText('not financial advice');
    }
  });
});
