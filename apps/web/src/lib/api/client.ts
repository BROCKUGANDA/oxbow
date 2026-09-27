/* =============================================================================
   The client. ONE unwrap point, ONE retry policy, ONE stale-response guard.

   ENVELOPE. `unwrap()` below is the only function in the app that reads `.data`
   or `.meta` off a response body, and the only place that would notice a
   `success` key if one ever appeared. Components receive `{ data, meta }` from a
   query hook and never touch the envelope, which is what makes "status lives in
   the HTTP code" a property of the codebase rather than a convention.

   RETRY. TanStack Query is configured from `isRetryable` in problem.ts: 5xx and
   the network path get exponential backoff with jitter, 4xx gets none. The
   `shouldRetry` predicate below is the whole policy and it is unit-tested,
   because "a retry loop around a 4xx" is a named rejection trigger rather than a
   style preference.

   STALE RESPONSES. Every request carries a sequence number scoped to its query
   key, and `staleGuard` discards any response whose sequence is older than the
   one already rendered. The failure it prevents is the visible one: filter the
   queue from A to B, B answers first, and A arrives late and paints itself over
   the screen while the filter control says B. That is `test_stale_response_discarded`.
   ============================================================================= */

import { QueryClient, type QueryKey } from '@tanstack/react-query';
import { type ContractViolation, type Decoder, decodeOrThrow } from '../codec';
import { type ListMeta, ListMetaDecoder } from './contract';
import { ApiError, type ApiFailure, isRetryable } from './problem';
import { asContractFailure, assertOk, getTransport, withInjectedFailure } from './transport';

/** The only success shape the client knows: the envelope's two keys, typed. */
export type Payload<T> = { data: T; meta: ListMeta };

const BASE_BACKOFF_MS = 250;
const MAX_BACKOFF_MS = 4000;
const MAX_RETRIES = 3;

/**
 * THE chokepoint. Validates the envelope shape, then decodes `data` against the
 * route's decoder and `meta` against the meta decoder. Anything that is not
 * exactly `{ data, meta }` is a contract failure with the offending key list in
 * the message, so a route that grows a `success` boolean or renames `data` fails
 * here rather than rendering a blank cell.
 */
export function unwrap<T>(body: unknown, dataDecoder: Decoder<T>, path: string): Payload<T> {
  const bad = (message: string): never => {
    throw new ApiError({
      kind: 'contract',
      class: 'contract',
      message,
      path,
      status: null,
      run_id: null,
      retry_after_ms: null,
    });
  };

  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    bad('expected an envelope object with exactly the keys data and meta');
  }

  const record = body as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  if (keys.length !== 2 || !keys.includes('data') || !keys.includes('meta')) {
    bad(`envelope keys were [${keys.join(', ')}], expected [data, meta]`);
  }

  try {
    return {
      data: decodeOrThrow(dataDecoder, record.data),
      meta: decodeOrThrow(ListMetaDecoder, record.meta),
    };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === 'ContractViolation') {
      throw new ApiError(asContractFailure(error as ContractViolation, path, null));
    }
    throw error;
  }
}

/** Builds the URL for a route with its query parameters, dropping absent values.
 *  An array value repeats the key (`band=A&band=B`), because the API's list filters
 *  are repeated query parameters, not comma-joined: a joined `band=A,B` reaches the
 *  server as one unknown band and answers 400 (apps/api/routers/alerts.py `_parse_bands`). */
export function withQuery(
  path: string,
  params: Record<string, string | number | boolean | readonly string[] | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      for (const entry of value) {
        if (entry.length > 0) search.append(key, entry);
      }
      continue;
    }
    search.set(key, String(value));
  }
  const query = search.toString();
  return query.length === 0 ? path : `${path}?${query}`;
}

/**
 * One request, decoded. `signal` is wired from Query so an abandoned observer
 * actually aborts the fetch rather than racing it.
 */
export async function request<T>(
  path: string,
  decoder: Decoder<T>,
  init: { method?: 'GET' | 'POST'; body?: unknown; signal?: AbortSignal } = {},
): Promise<Payload<T>> {
  const transport = await getTransport();
  /* The only place the page's own query string can influence an API request, and only
     in the dev failure-injection mode — see `withInjectedFailure`. */
  const requestPath = transport.mode === 'problem' ? withInjectedFailure(path) : path;
  const response = await transport.send({
    path: requestPath,
    method: init.method ?? 'GET',
    body: init.body,
    signal: init.signal,
  });
  const ok = assertOk(response, requestPath);
  return unwrap(ok, decoder, requestPath);
}

/* ---------------------------------------------------------------- retry ---- */

/** Exponential backoff with full jitter, on the 1s/4s/16s shape §13 uses for the
 *  outbox so the two retry schedules in the product are recognisably the same. */
export function backoffMs(attempt: number, failure: ApiFailure): number {
  const wait = failure.kind === 'problem' ? failure.retry_after_ms : null;
  if (wait !== null && wait !== undefined && Number.isFinite(wait)) return wait;
  const base = Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
  return base / 2 + Math.random() * (base / 2);
}

/**
 * The retry predicate. Exported so the unit test asserts the 4xx half of it
 * directly instead of trusting a comment.
 */
export const retryPolicy: (failureCount: number, error: unknown) => boolean = (failureCount, error) => {
  const failure = error instanceof ApiError ? error.failure : null;
  if (failure === null) return false;
  if (failureCount >= MAX_RETRIES) return false;
  return isRetryable(failure);
};

export function createQueryClient(options: { staleTimeMs?: number } = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: retryPolicy,
        retryDelay: (attempt, error) => {
          const failure = error instanceof ApiError ? error.failure : null;
          return failure === null ? attempt * 1000 : backoffMs(attempt, failure);
        },
        // Refetch-on-window-focus would re-run a server allocation every time the
        // judge tabs back to the browser; the queue is a pinned run, not a live feed.
        refetchOnWindowFocus: false,
        staleTime: options.staleTimeMs ?? 30_000,
        gcTime: 5 * 60_000,
      },
      mutations: { retry: false },
    },
  });
}

/* -------------------------------------------------- stale-response guard --- */

/**
 * A per-key sequence counter. `begin()` stamps a request, `isStale()` answers after
 * the await: if a newer request has been stamped in the meantime, this response is
 * from a superseded query and must be thrown away, not rendered.
 */
const sequences = new Map<string, number>();

export function begin(key: string): number {
  const next = (sequences.get(key) ?? 0) + 1;
  sequences.set(key, next);
  return next;
}

export function isStale(key: string, seq: number): boolean {
  return (sequences.get(key) ?? seq) > seq;
}

/** Query keys are the array form; this makes one for a route plus its params. */
export function queryKey(route: string, params: Record<string, unknown>): QueryKey {
  return [route, ...Object.entries(params).map(([key, value]) => `${key}=${String(value)}`)];
}
