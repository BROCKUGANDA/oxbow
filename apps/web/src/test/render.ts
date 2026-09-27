import { act } from 'react';
import type { ReactElement } from 'react';
import { type Root, createRoot } from 'react-dom/client';

/**
 * The minimal mount helper the edge tests need — react-dom/client + React.act,
 * no extra dependencies (nothing new gets installed in this repo). One container
 * per call; `cleanup` unmounts and removes it.
 */
type RenderResult = {
  container: HTMLElement;
  cleanup: () => void;
  rerender: (element: ReactElement) => void;
};

const live: Root[] = [];

export function render(element: ReactElement): RenderResult {
  const container = document.createElement('div');
  document.body.appendChild(container);
  // The root is created outside `act` and only its render/unmount calls are inside, so
  // nothing has to claim a value assigned in a closure TypeScript cannot see. The three
  // `as Root` casts this function used to carry existed only to say "not null, trust me".
  const root = createRoot(container);
  act(() => {
    root.render(element);
  });
  live.push(root);
  return {
    container,
    rerender: (next: ReactElement): void => {
      act(() => {
        root.render(next);
      });
    },
    cleanup: (): void => {
      act(() => {
        root.unmount();
      });
      container.remove();
      const index = live.indexOf(root);
      if (index >= 0) live.splice(index, 1);
    },
  };
}

/** Flush pending effects/timers inside act so state updates land before asserts. */
export function flush(callback?: () => void): void {
  if (callback === undefined) {
    act(() => {
      /* no-op: drains the effect queue */
    });
    return;
  }
  act(() => {
    callback();
  });
}

export function text(container: HTMLElement): string {
  return container.textContent ?? '';
}

export function click(container: HTMLElement, selector: string): void {
  const target = container.querySelector<HTMLElement>(selector);
  if (target === null) throw new Error(`no element for selector ${selector}`);
  act(() => {
    target.click();
  });
}

/** Fails loudly rather than silently asserting nothing. */
export function mustFind<T extends Element>(container: HTMLElement, selector: string): T {
  const found = container.querySelector<T>(selector);
  if (found === null) throw new Error(`expected exactly one ${selector} in:\n${container.outerHTML.slice(0, 2000)}`);
  return found;
}

export function mustFindAll<T extends Element>(container: HTMLElement, selector: string): T[] {
  const found = Array.from(container.querySelectorAll<T>(selector));
  if (found.length === 0)
    throw new Error(`expected at least one ${selector} in:\n${container.outerHTML.slice(0, 2000)}`);
  return found;
}

/** Unmounts every root created through `render`; call from afterEach. */
export function cleanupAll(): void {
  while (live.length > 0) {
    const root = live.pop();
    if (root !== undefined) {
      act(() => {
        root.unmount();
      });
    }
  }
  document.body.replaceChildren();
}
