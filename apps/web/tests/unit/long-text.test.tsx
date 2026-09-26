/**
 * §14 `test_long_text_truncated_and_escaped` — markup-shaped text rendered as text:
 * truncated visibly, full value retained in the title attribute, and never becoming
 * an element. Strict escaping is React's by default; the assertion that no `<img>`
 * or `<script>` node exists is what makes "escaped" a tested fact rather than a hope.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { HOSTILE_TEXT, LongTextRow } from '@/app/dev/states/gallery-rows';
import { cleanupAll, mustFind, render } from '@/test/render';

afterEach(cleanupAll);

describe('long, markup-shaped text', () => {
  it('renders as text: the element is a <p>, and no node from the payload exists', () => {
    const view = render(<LongTextRow />);
    const paragraph = mustFind<HTMLParagraphElement>(view.container, 'p[data-long-text]');
    expect(paragraph.tagName).toBe('P');
    expect(view.container.querySelector('img')).toBeNull();
    expect(view.container.querySelector('script')).toBeNull();
    expect(view.container.querySelectorAll('*').length).toBeGreaterThan(0);
    view.cleanup();
  });

  it('carries the whole value in the title attribute, so truncation never loses the fact', () => {
    const view = render(<LongTextRow />);
    const paragraph = mustFind<HTMLParagraphElement>(view.container, 'p[data-long-text]');
    expect(paragraph.getAttribute('title')).toBe(HOSTILE_TEXT);
    expect(paragraph.textContent).toBe(HOSTILE_TEXT);
    expect(HOSTILE_TEXT).toContain('<img src=x onerror="alert(1)">');
    view.cleanup();
  });

  it('is visibly truncated with the ellipsis utility rather than wrapping forever', () => {
    const view = render(<LongTextRow />);
    const paragraph = mustFind<HTMLParagraphElement>(view.container, 'p[data-long-text]');
    expect(paragraph.style.textOverflow).toBe('ellipsis');
    expect(paragraph.style.overflow).toBe('hidden');
    view.cleanup();
  });
});
