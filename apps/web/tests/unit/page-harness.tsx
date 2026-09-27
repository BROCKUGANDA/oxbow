/** Test harness for mounting a route through the REAL client stack.
 *
 *  Nothing here mocks a hook or a component. The page mounts inside a
 *  `QueryClientProvider`, the transport is the app's own `api` mode, and the only thing
 *  substituted is the network: `globalThis.fetch` is answered by a router that can hand
 *  back either the developer-contract fixture or a byte-for-byte server shape from
 *  `src/fixtures/server-shape.fixture.ts`. That keeps the claims under test honest —
 *  "the page posted to this URL with this body" is read off the request the browser
 *  would have put on the wire, and every response has to survive `unwrap()` and the
 *  route's decoder, exactly as a live one does.
 *
 *  Retries are off so a deliberate 4xx/5xx stays one request; everything else about the
 *  client is the shipped configuration (`createQueryClient`).
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { send as fixtureSend } from '@/fixtures/transport.fixture';
import type { TransportRequest, TransportResponse } from '@/lib/api/transport';
import { type RenderResult, render } from '@/test/render';

export type { TransportRequest, TransportResponse };

/** What the page actually put on the wire, in order. */
export type RecordedRequest = { path: string; method: string; body: unknown };

/** `?query` included, matching what the transport builds. */
type Router = (request: TransportRequest) => TransportResponse | Promise<TransportResponse>;

/** The default router: the developer-contract fixture answers everything. */
export const fixtureRouter: Router = (request) => fixtureSend(request);

function responseShim(response: TransportResponse): unknown {
  const text = JSON.stringify(response.body ?? null);
  return {
    status: response.status,
    ok: response.status >= 200 && response.status < 300,
    headers: {
      get: (name: string): string | null => (name.toLowerCase() === 'content-type' ? response.contentType : null),
    },
    text: async (): Promise<string> => text,
    json: async (): Promise<unknown> => JSON.parse(text),
  };
}

export type Mounted = RenderResult & {
  /** Every request the page made, in order, with its parsed body. */
  requests: () => RecordedRequest[];
  /** The last request to a path containing `needle`, or null. */
  lastRequestTo: (needle: string) => RecordedRequest | null;
};

/**
 * Mount `ui` against a fetch stub. `NEXT_PUBLIC_OXBOW_TRANSPORT` is forced to `api` so
 * `resolveMode()` picks the real transport rather than the lazily-imported fixture one,
 * and the stub is installed before the first request so the transport's cached bearer
 * mint goes through it too.
 */
export function mountWithApi(ui: ReactElement, router: Router = fixtureRouter): Mounted {
  process.env.NEXT_PUBLIC_OXBOW_TRANSPORT = 'api';
  const requests: RecordedRequest[] = [];
  const stub = vi.fn(async (input: unknown, init: unknown): Promise<unknown> => {
    const url = String(input);
    const options = (init ?? {}) as { method?: string; body?: string };
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    const body: unknown =
      typeof options.body === 'string' && options.body.length > 0 ? JSON.parse(options.body) : undefined;
    const request: TransportRequest = {
      path,
      method: (options.method ?? 'GET').toUpperCase() === 'POST' ? 'POST' : 'GET',
      body,
    };
    requests.push({ path, method: request.method, body });
    return responseShim(await router(request));
  });
  vi.stubGlobal('fetch', stub);

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      {/* One client per mount: `mintedToken` is cached per process, so the demo mint
          from an earlier mount would otherwise ride a dead token into this one. */}
      {ui}
    </QueryClientProvider>,
  );
  return {
    ...view,
    requests: () => requests.slice(),
    lastRequestTo: (needle: string) => [...requests].reverse().find((entry) => entry.path.includes(needle)) ?? null,
  };
}

/** Drain the query/mutation promise chain into `act` so state lands before asserting. */
export async function settle(rounds = 6): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await act(async () => {
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

/** Click a control — by selector or by the element itself — and let whatever it started
 *  finish, including the optimistic transition and the invalidation it triggers. */
export async function press(scope: HTMLElement, target: string | Element): Promise<void> {
  const element = typeof target === 'string' ? scope.querySelector<HTMLElement>(target) : (target as HTMLElement);
  if (element === null || element === undefined) {
    throw new Error(`no element for ${typeof target === 'string' ? target : 'the element passed'}`);
  }
  await act(async () => {
    element.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await Promise.resolve();
  });
  await settle();
}

/** Set a controlled textarea the way the DOM does it, so React sees a real change. */
export async function typeInto(element: HTMLElement, value: string): Promise<void> {
  const proto = element instanceof window.HTMLTextAreaElement ? window.HTMLTextAreaElement : window.HTMLInputElement;
  const descriptor = Object.getOwnPropertyDescriptor(proto.prototype, 'value');
  if (descriptor?.set === undefined) throw new Error('no value setter on this element kind');
  descriptor.set.call(element, value);
  await act(async () => {
    element.dispatchEvent(new window.Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

/** Find a button by its exact label inside a mounted tree. */
export function buttonByText(scope: HTMLElement, label: string): HTMLElement {
  const found = Array.from(scope.querySelectorAll('button')).find((node) => node.textContent === label);
  if (found === undefined) throw new Error(`no button labelled “${label}” in:\n${scope.textContent?.slice(0, 800)}`);
  return found;
}

/** The transport shape for a JSON answer, mirroring what `fetch` would have returned. */
export function json(status: number, body: unknown): TransportResponse {
  return { status, contentType: 'application/json', body, retryAfterMs: null };
}

/** A `problem+json` answer, with the media type the client reads the status from. */
export function problem(
  status: number,
  type: string,
  title: string,
  detail: string,
  extra: Record<string, unknown> = {},
): TransportResponse {
  return {
    status,
    contentType: 'application/problem+json',
    retryAfterMs: null,
    body: {
      type,
      title,
      status,
      detail,
      instance: '/api/…',
      run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
      trace_id: '01J4Z7TRACE00000000000000',
      retryable: false,
      ...extra,
    },
  };
}

export function teardown(view: Mounted): void {
  view.cleanup();
  vi.unstubAllGlobals();
}
