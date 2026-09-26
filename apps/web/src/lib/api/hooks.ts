/* =============================================================================
   Query hooks. One wrapper, and it is the only way a screen reads the API.

   The wrapper does four things a bare `useQuery` would leave to each call site:
   * decodes through the route's contract, so `data` is typed and validated;
   * carries `meta` beside it, because provenance, run id and assumptions are part
     of the payload and not a side channel;
   * guards stale responses per query key, so a superseded request can never paint
     over the answer that replaced it;
   * exposes `failure` as the typed union, so a pane renders an error tier without
     touching `instanceof`.

   A screen never imports `request` directly. That single discipline is what makes
   the "no route renders a value that is not in the API response" claim checkable:
   the only path from the network to a component is through a decoder.
   ============================================================================= */

'use client';

import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import type { Decoder } from '../codec';
import { ROUTES, type RuntimeMeta } from './contract';
import { begin, isStale, request, withQuery } from './client';
import type { ListMeta } from './contract';
import { ApiError, type ApiFailure } from './problem';

export type QueryState<T> = {
  data: T | null;
  meta: ListMeta | null;
  failure: ApiFailure | null;
  isPending: boolean;
  isPlaceholderData: boolean;
  /** True while a retry is in flight, so a retry button can say so without a spinner. */
  isFetching: boolean;
  /** Attempt count, for ErrorPane's "attempt N". */
  attempts: number;
  refetch: () => Promise<unknown>;
};

function toFailure(error: unknown): ApiFailure | null {
  if (error instanceof ApiError) return error.failure;
  if (error instanceof Error) {
    return {
      kind: 'network',
      class: 'network',
      message: error.message,
      status: null,
      run_id: null,
      retry_after_ms: null,
    };
  }
  return null;
}

export function useResource<T>(
  route: string,
  path: string,
  decoder: Decoder<T>,
  options: { enabled?: boolean; keepPreviousData?: boolean } = {},
): QueryState<T> {
  const key: QueryKey = [route, path];
  const query = useQuery({
    queryKey: key,
    enabled: options.enabled ?? true,
    // keepPreviousData is what stops the queue from blanking to a skeleton on every
    // filter change; the geometry is unchanged, so nothing shifts.
    placeholderData: options.keepPreviousData === false ? undefined : keepPreviousData,
    queryFn: async ({ signal }) => {
      const seq = begin(route);
      const payload = await request(path, decoder, { signal });
      if (isStale(route, seq)) throw new StaleResponseError(route, seq);
      return payload;
    },
  });

  const failure = toFailure(query.error);
  /* TanStack counts *failures*: after the first attempt throws, failureCount is 1.
     The label is the number of requests made, so one failed attempt reads
     "attempt 1" — not "attempt 2", which is what a bare `failureCount + 1` showed
     for every single-shot 4xx. */
  const attempts = Math.max(query.failureCount, 1);

  return {
    data: query.data?.data ?? null,
    meta: query.data?.meta ?? null,
    failure,
    isPending: query.isPending,
    isPlaceholderData: query.isPlaceholderData,
    isFetching: query.isFetching,
    attempts,
    refetch: query.refetch,
  };
}

/** Thrown internally when a response has been superseded. It is deliberately NOT
 *  surfaced as an error state: the newer request is already in flight, and a stale
 *  discard is normal operation, not a failure the analyst needs to see. */
export class StaleResponseError extends Error {
  constructor(route: string, seq: number) {
    super(`${route} response ${String(seq)} superseded by a newer request`);
    this.name = 'StaleResponseError';
  }
}

export function useListResource<T>(
  route: string,
  path: string,
  decoder: Decoder<T>,
  params: Record<string, string | number | boolean | readonly string[] | null | undefined>,
  options: { enabled?: boolean; keepPreviousData?: boolean } = {},
): QueryState<T> {
  return useResource(route, withQuery(path, params), decoder, options);
}

/** A write: invalidates its own key and nothing else, so a decision lands in one
 *  pane while every other pane keeps whatever it already resolved. */
export function useWrite<TBody, TReceipt>(path: string, decoder: Decoder<TReceipt>, invalidate: QueryKey[]) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async (body: TBody) => request(path, decoder, { method: 'POST', body }),
    onSuccess: () => {
      for (const key of invalidate) void queryClient.invalidateQueries({ queryKey: key });
    },
  });
  return {
    mutate: mutation.mutate,
    mutateAsync: mutation.mutateAsync,
    pending: mutation.isPending,
    failure: toFailure(mutation.error),
    reset: mutation.reset,
    receipt: mutation.data ?? null,
  };
}

/* --------------------------------------------------------- derived state -- */

/** The deployment facts every renderer needs — timezone, currency, economics — from
 *  `GET /api/meta/run`. A screen never types a zone or a UTC offset itself. */
export function useRuntime(): QueryState<RuntimeMeta> {
  return useResource('runtime', ROUTES.runtime.path, ROUTES.runtime.data);
}

/** Copy-to-clipboard with the confirmation living in the button's own label. */
export function useClipboard(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  const copy = (text: string): void => {
    if (typeof navigator === 'undefined' || navigator.clipboard === undefined) return;
    void navigator.clipboard.writeText(text);
    setCopied(true);
  };
  return [copied, copy];
}
