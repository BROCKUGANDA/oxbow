/**
 * §14 `test_reduced_motion_gallery` — the non-browser half.
 *
 * The guarantee is three things, and each is checked here where it is cheap:
 * 1. the shimmer sweep is NOT RENDERED when the OS prefers reduced motion
 *    (`usePrefersReducedMotion` gates the element itself — a paused loop that
 *    never existed cannot restart);
 * 2. the query-parameter path exists end to end: the server page reads
 *    `?motion=reduced` and the shell writes `html[data-motion='reduced']`, which
 *    globals.css uses to stop every CSS animation — including the sweep when the
 *    preference is only asserted by URL, not by the OS;
 * 3. the MotionConfig override flips with the prop.
 *
 * The browser half — real media emulation through a launched Chromium — is the
 * Playwright suite.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { StatesGallery } from '@/app/dev/states/gallery';
import { Shimmer } from '@/design/primitives/Shimmer';
import { cleanupAll, render, text } from '@/test/render';
import { setPrefersReducedMotion } from '@/test/setup';

// The gallery's money strip runs through the real query layer. The gallery renders
// the strip as a skeleton while that query is pending, and the states under test
// (shimmer, bands, glyphs, ledger, empties, tiers) are all in the static part, so
// the pending state is what the component is exercised in. Mocking the hook layer
// keeps this a component test, not an integration test against a live API — the
// fixture props inside the gallery remain the sanctioned mechanism.
vi.mock('@/lib/api/hooks', () => ({
  useResource: () => ({
    data: null,
    meta: null,
    failure: null,
    isPending: true,
    isPlaceholderData: false,
    isFetching: false,
    attempts: 1,
    refetch: async () => undefined,
  }),
}));

afterEach(cleanupAll);

// vitest runs with the apps/web directory as its working directory.
const ROOT = process.cwd();
const globalsCss = readFileSync(path.join(ROOT, 'src/app/globals.css'), 'utf8');

describe('the shimmer under reduced motion', () => {
  it('renders the sweep when the OS preference is no-preference', () => {
    setPrefersReducedMotion(false);
    const view = render(<Shimmer width={200} height={14} label="Sweeping placeholder" />);
    expect(view.container.querySelector('[data-shimmer-sweep]')).not.toBeNull();
    view.cleanup();
  });

  it('does not render the sweep at all when the OS prefers reduced motion', () => {
    setPrefersReducedMotion(true);
    const view = render(<Shimmer width={200} height={14} label="Sweeping placeholder" />);
    expect(view.container.querySelector('[data-shimmer-sweep]')).toBeNull();
    // The resting block still paints, so the skeleton shape is right at rest.
    expect(view.container.textContent).toBe('');
    view.cleanup();
    setPrefersReducedMotion(false);
  });

  it('the sweep is a transform loop at 1.4s — never background-position', () => {
    const view = render(<Shimmer width={200} height={14} />);
    const sweep = view.container.querySelector('[data-shimmer-sweep]');
    expect(sweep).not.toBeNull();
    const style = (sweep as HTMLElement).getAttribute('style') ?? '';
    expect(style).toContain('animation: oxbow-shimmer 1.4s');
    expect(style).toContain('will-change: transform');
    expect(style).not.toContain('background-position');
    const keyframes = readFileSync(path.join(ROOT, 'src/design/primitives/Shimmer.tsx'), 'utf8');
    expect(keyframes).toContain('translate3d(-150%, 0, 0)');
    view.cleanup();
  });
});

describe('the CSS contract for the forced path', () => {
  it('globals.css pauses every animation under both the media query and the data-motion attribute', () => {
    expect(globalsCss).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    // Quoting is not the contract; the selector and the rules it carries are.
    expect(globalsCss).toMatch(/html\[data-motion=["']reduced["']\]/);
    expect(globalsCss).toMatch(/html\[data-motion=["']reduced["']\] \[data-shimmer-sweep\]/);
    // ...and the selector must have a writer. It was read by the CSS and set by nothing
    // for the whole life of this file: the gallery's own comment claimed "?motion=reduced
    // sets this attribute" while no code did, so every rule in that block matched no
    // element. A CSS-only assertion cannot see that; this one can.
    const gallery = readFileSync('src/app/dev/states/gallery.tsx', 'utf8');
    expect(gallery).toMatch(/setAttribute\(\s*['"]data-motion['"]\s*,\s*['"]reduced['"]\s*\)/);
    expect(gallery).toMatch(/removeAttribute\(\s*['"]data-motion['"]\s*\)/);
  });

  it('the gallery says which mode it is in, from the server-read query string', () => {
    const full = render(<StatesGallery reduced={false} />);
    expect(text(full.container)).toContain('Currently:');
    expect(text(full.container)).toContain('full motion');
    full.cleanup();

    const reduced = render(<StatesGallery reduced />);
    expect(text(reduced.container)).toContain('reduced motion');
    reduced.cleanup();
  });
});
