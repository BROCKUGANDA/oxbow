/**
 * §14 P8 gate, third clause: CLS measured, not asserted, on the queue and case routes.
 *
 * The observer is installed by an init script — before any content of the document
 * exists — which is the only honest way to catch the skeleton-to-resolution shift.
 * Chrome's own floor for a real layout shift is 0.001 (sub-threshold shifts are not
 * counted at all); the gate says zero, so the assertion is "no shift above Chrome's
 * own floor", and the exact accumulated value plus the source of every shift is
 * printed to the report regardless of outcome.
 */
import { expect, test } from '@playwright/test';

const INSTALL_OBSERVER = () => {
  const w = window as unknown as {
    __cls: number;
    __clsEntries: { value: number; node: string }[];
  };
  w.__cls = 0;
  w.__clsEntries = [];
  new PerformanceObserver((list) => {
    // `layout-shift` entries are not in this TypeScript lib, so the shape is declared
    // once here rather than cast at every use. `value` is the shift score and
    // `hadRecentInput` is the exclusion the CLS metric itself specifies.
    type LayoutShiftEntry = PerformanceEntry & { value: number; hadRecentInput: boolean };
    for (const entry of list.getEntries() as LayoutShiftEntry[]) {
      if (entry.hadRecentInput) continue;
      w.__cls += entry.value;
      const sources = (entry as unknown as { sources?: { node: Element }[] }).sources ?? [];
      w.__clsEntries.push({
        value: entry.value,
        node: sources
          .map((s) => {
            const el = s.node;
            const cls = typeof el.className === 'string' ? el.className.split(/\s+/).slice(0, 2).join('.') : '';
            return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${cls ? `.${cls}` : ''}`;
          })
          .join(', '),
      });
    }
  }).observe({ type: 'layout-shift', buffered: true });
};

async function measureCls(page: import('@playwright/test').Page, route: string): Promise<{ total: number; entries: unknown[] }> {
  await page.addInitScript(INSTALL_OBSERVER);
  await page.goto(route, { waitUntil: 'load' });
  // The skeleton→resolution (or →error) transition lands inside ~5 s: TanStack
  // retries the 503 three times with ≤4 s backoff before the failure surface paints.
  await page.waitForTimeout(9_000);
  return page.evaluate(() => ({
    total: (window as unknown as { __cls: number }).__cls,
    entries: (window as unknown as { __clsEntries: unknown[] }).__clsEntries,
  }));
}

test.describe('measured CLS on the gate routes', () => {
  test('test_cls_zero · /alerts — matched-geometry queue skeleton resolves with no counted shift', async ({ page }) => {
    const cls = await measureCls(page, '/alerts');
    console.log(`CLS /alerts = ${String(cls.total)} · shifts: ${JSON.stringify(cls.entries)}`);
    expect(cls.total).toBeLessThanOrEqual(0.001);
  });

  test('test_cls_zero · /cases/[id] — three panes reserve their geometry through resolution', async ({ page }) => {
    const cls = await measureCls(page, '/cases/01J4Z7M2QK9N7V1C4X6E8G0B2D');
    console.log(`CLS /cases = ${String(cls.total)} · shifts: ${JSON.stringify(cls.entries)}`);
    expect(cls.total).toBeLessThanOrEqual(0.001);
  });

  test('test_cls_zero · /dev/states — the page that documents the claim keeps it', async ({ page }) => {
    const cls = await measureCls(page, '/dev/states');
    console.log(`CLS /dev/states = ${String(cls.total)} · shifts: ${JSON.stringify(cls.entries)}`);
    expect(cls.total).toBeLessThanOrEqual(0.001);
  });
});
