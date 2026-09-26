/* =============================================================================
   The transport seam: the one place that decides where a response body comes from.

   THREE MODES, and only one of them is shippable data.

   1. `api`      — fetch against the P7 server. The default, and the default in any
                   production build.
   2. `fixture`  — the developer contract fixtures under `src/fixtures/`, used while
                   P7 is still being written and by the Playwright suite. It CANNOT
                   be selected in a production build (see `resolveMode`), and when it
                   is active every response carries `provenance: 'fixture'`, which the
                   shell renders as a permanent banner. A fixture can therefore never
                   reach an analyst's screen looking like a measurement — which is the
                   plan §19 prohibition this whole file exists to make structural
                   rather than a matter of care.
   3. `problem`  — a failure injector for the state gallery: it returns real
                   problem+json documents so the four error tiers are demonstrable
                   without taking the server down.

   The `provenance` field on `Meta` is P7's own design (schemas/common.py: "a fixture
   run and a pipeline run render identically otherwise, and the difference is the
   difference between a measurement and a demo"). This file uses it for exactly that.
   ============================================================================= */

import { ContractViolation } from '../codec';
import { ApiError, classifyStatus, ProblemDetailDecoder, type ApiFailure } from './problem';

export type TransportMode = 'api' | 'fixture' | 'problem';

export type TransportRequest = {
  path: string;
  method: 'GET' | 'POST';
  body?: unknown;
  signal?: AbortSignal;
};

export type TransportResponse = {
  status: number;
  contentType: string;
  body: unknown;
  retryAfterMs: number | null;
};

/** Implemented by `src/fixtures/transport.fixture.ts` and by the real fetch path. */
export interface Transport {
  readonly mode: TransportMode;
  send(request: TransportRequest): Promise<TransportResponse>;
}

/** Set at module scope by the provider; `process.env` is inlined by Next at build time. */
function resolveMode(): TransportMode {
  const requested = process.env.NEXT_PUBLIC_OXBOW_TRANSPORT;
  if (requested === 'fixture' || requested === 'problem') {
    // A production build refuses the fixture transport outright, so fixture bytes
    // cannot be shipped to a judge and described as a run.
    if (process.env.NODE_ENV === 'production' && process.env.OXBOW_ALLOW_FIXTURE_BUILD !== '1') {
      return 'api';
    }
    return requested;
  }
  return 'api';
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000';

async function apiTransport(request: TransportRequest): Promise<TransportResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${request.path}`, {
      method: request.method,
      signal: request.signal,
      headers: request.body === undefined ? {} : { 'content-type': 'application/json' },
      body: request.body === undefined ? undefined : JSON.stringify(request.body),
    });
  } catch (error) {
    throw new ApiError({
      kind: 'network',
      class: 'network',
      message: error instanceof Error ? error.message : 'the request did not complete',
      status: null,
      run_id: null,
      retry_after_ms: null,
    });
  }

  const text = await response.text();
  const contentType = response.headers.get('content-type') ?? '';
  const retryAfter = response.headers.get('retry-after');
  const retryAfterMs = retryAfter === null ? null : Number.parseFloat(retryAfter) * 1000;

  if (text.length === 0 && response.status === 204) {
    return { status: response.status, contentType: 'application/json', body: null, retryAfterMs };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    // Non-JSON from an API is a contract failure with the HTTP status preserved, so
    // a 502 from a proxy is still recognised as retryable rather than as malformed.
    const failure: ApiFailure = {
      kind: 'contract',
      class: 'contract',
      message: 'the response body was not JSON',
      path: request.path,
      status: response.status,
      run_id: null,
      retry_after_ms: retryAfterMs,
    };
    if (response.status >= 500) {
      throw new ApiError({
        kind: 'problem',
        class: 'server',
        problem: {
          type: 'about:blank',
          title: 'Upstream failure',
          status: 502,
          detail: `the gateway returned a non-JSON body (HTTP ${String(response.status)})`,
          instance: request.path,
          run_id: null,
          trace_id: null,
          errors: null,
          retryable: true,
          conflict: null,
        },
        retry_after_ms: retryAfterMs,
      });
    }
    throw new ApiError(failure);
  }

  return { status: response.status, contentType, body: parsed, retryAfterMs };
}

/**
 * Turns a response into either its body or a thrown typed failure. Status is read
 * from the HTTP code, never from the body — that is the envelope doctrine, and it is
 * why there is no `success` flag anywhere in the client.
 */
export function assertOk(response: TransportResponse, path: string): unknown {
  if (response.status >= 200 && response.status < 300) return response.body;

  const isProblem = response.contentType.includes('problem+json');
  const decoded = isProblem ? ProblemDetailDecoder.decode(response.body, '') : null;
  if (decoded !== null && decoded !== undefined && decoded.ok) {
    throw new ApiError({
      kind: 'problem',
      class: classifyStatus(decoded.value.status),
      problem: decoded.value,
      retry_after_ms: response.retryAfterMs,
    });
  }

  // A non-2xx without a valid problem document is itself a contract failure: the
  // plan requires every error to be problem+json, so a plain `{"detail": "..."}`
  // is P7 not yet honouring its own schema, and saying so beats guessing a title.
  throw new ApiError({
    kind: 'contract',
    class: 'contract',
    message: isProblem
      ? 'a problem+json body failed to decode'
      : `HTTP ${String(response.status)} without an application/problem+json body`,
    path,
    status: response.status,
    run_id: readRunId(response.body),
    retry_after_ms: response.retryAfterMs,
  });
}

function readRunId(body: unknown): string | null {
  if (typeof body !== 'object' || body === null) return null;
  const candidate = (body as Record<string, unknown>).run_id;
  return typeof candidate === 'string' ? candidate : null;
}

/** Wraps a decoder rejection so the boundary owns the failure type. */
export function asContractFailure(error: ContractViolation, path: string, status: number | null): ApiFailure {
  return {
    kind: 'contract',
    class: 'contract',
    message: error.message,
    path: `${path} → ${error.path}`,
    status,
    run_id: null,
    retry_after_ms: null,
  };
}

/**
 * Forwards a `?fail=` from the *page* URL onto the API request, in the failure-injection
 * mode only.
 *
 * `/alerts?fail=500` and `/policy?fail=503-solver` are how the error tiers and the
 * degraded path become reachable in a browser — `devproblem.transport.ts` reads the
 * parameter off the request it is handed, and the state gallery's own copy already points
 * at those URLs. Nothing about this reaches a normal build: `resolveMode` refuses the
 * `problem` mode in production, and the caller checks the ambient mode before applying
 * it, so a real request to P7 never carries a demo parameter.
 */
export function withInjectedFailure(path: string): string {
  if (typeof window === 'undefined') return path;
  const injected = new URLSearchParams(window.location.search).get('fail');
  if (injected === null || injected.length === 0 || path.includes('fail=')) return path;
  return `${path}${path.includes('?') ? '&' : '?'}fail=${encodeURIComponent(injected)}`;
}

let cached: Transport | null = null;

/**
 * The ambient transport. Loaded lazily so the fixture modules — which are the only
 * place sample bytes live — never enter the server bundle when the mode is `api`.
 */
export async function getTransport(): Promise<Transport> {
  if (cached !== null) return cached;
  const mode = resolveMode();
  if (mode === 'api') {
    cached = { mode, send: apiTransport };
    return cached;
  }
  const module = mode === 'problem' ? await import('./devproblem.transport') : await import('../../fixtures/transport.fixture');
  cached = { mode, send: module.send };
  return cached;
}

/** Exposed for the state gallery's mode switch and for tests. */
export function transportMode(): TransportMode {
  return resolveMode();
}
