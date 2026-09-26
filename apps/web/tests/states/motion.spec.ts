/**
 * §14 P8 gate, fifth clause: `prefers-reduced-motion` as a tested path.
 *
 * Two independent paths, both driven through a real browser:
 * 1. the OS media preference, emulated at the engine level — the shimmer sweep must
 *    not exist at all (the component gates the element on the preference);
 * 2. `/dev/states?motion=reduced` — the URL path the plan names, where the sweep
 *    elements are present but every CSS animation is pinned off, including the
 *    sweep's `transform` loop.
 */
import { expect, test } from '@playwright/test';

const sweepState = () => {
  const sweeps = Array.from(document.querySelectorAll('[data-shimmer-sweep]'));
  const running = sweeps.filter((el) => {
    const cs = getComputedStyle(el);
    return cs.animationName !== 'none' && parseFloat(cs.animationDuration) > 0.01;
  });
  return {
    sweeps: sweeps.length,
    running: running.length,
    dataMotion: document.documentElement.dataset.motion ?? null,
    matches: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  };
};

test.describe('prefers-reduced-motion, emulated', () => {
  test('OS preference: the gallery renders zero sweep elements', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/dev/states', { waitUntil: 'load' });
    await expect(page.getByRole('heading', { name: 'State gallery' })).toBeVisible();
    const state = await page.evaluate(sweepState);
    expect(state.matches).toBe(true);
    expect(state.sweeps).toBe(0);
    expect(state.running).toBe(0);
  });

  test('no preference: the sweep exists and runs the 1.4s transform loop', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.goto('/dev/states', { waitUntil: 'load' });
    await page.waitForTimeout(1_500);
    const state = await page.evaluate(sweepState);
    expect(state.sweeps).toBeGreaterThan(40);
    expect(state.running).toBe(state.sweeps);
  });

  test('test_reduced_motion_gallery · ?motion=reduced pauses the sweep without the OS setting', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.goto('/dev/states?motion=reduced', { waitUntil: 'load' });
    await expect(page.getByText('Currently:')).toContainText('reduced motion');
    const state = await page.evaluate(sweepState);
    expect(state.dataMotion).toBe('reduced');
    expect(state.matches).toBe(false); // OS said no-preference…
    expect(state.running).toBe(0); // …and the gallery still stopped every loop.
    // the header of the page states the mode it painted in — the server read it,
    // the client did not fetch-and-swap (that swap was the 0.138 CLS this split fixed).
    await expect(page.locator('section[data-gallery-section="loading"]')).toBeVisible();
  });

  test('the reduced-motion query path costs the page no geometry it did not already have', async ({ page }) => {
    // Both modes of the same page must end at the same document height.
    await page.goto('/dev/states', { waitUntil: 'load' });
    await page.waitForTimeout(2_000);
    const full = await page.evaluate(() => document.body.scrollHeight);
    await page.goto('/dev/states?motion=reduced', { waitUntil: 'load' });
    await page.waitForTimeout(2_000);
    const reduced = await page.evaluate(() => document.body.scrollHeight);
    expect(reduced).toBe(full);
  });
});
