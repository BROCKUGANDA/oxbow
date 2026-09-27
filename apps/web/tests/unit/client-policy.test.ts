/**
 * §14 state-craft clause "TanStack Query retries 5xx with exponential backoff and
 * never retries 4xx" and the envelope doctrine the client enforces at its single
 * unwrap point. Both are pure policy and belong in a unit test, not a screenshot.
 */
import { describe, expect, it } from 'vitest';

import { backoffMs, retryPolicy, unwrap } from '@/lib/api/client';
import { DashboardDecoder } from '@/lib/api/contract';
import { ApiError, type ApiFailure, type ProblemStatus, isRetryable } from '@/lib/api/problem';

function problem(status: ProblemStatus, retryable: boolean): ApiFailure {
  return {
    kind: 'problem',
    class: status >= 500 ? 'server' : 'validation',
    problem: {
      type: 'about:blank',
      title: 't',
      status,
      detail: null,
      instance: null,
      run_id: null,
      trace_id: null,
      errors: null,
      retryable,
      conflict: null,
    },
    retry_after_ms: null,
  };
}

function captureThrow(fn: () => unknown): Error | null {
  try {
    fn();
    return null;
  } catch (error) {
    return error instanceof Error ? error : new Error(String(error));
  }
}

describe('the retry policy', () => {
  it('retries a retryable 5xx and honours Retry-After when the server sends one', () => {
    expect(isRetryable(problem(503, true))).toBe(true);
    const failure = { ...problem(503, true), retry_after_ms: 1200 } as ApiFailure;
    expect(backoffMs(0, failure)).toBe(1200);
  });

  it('never retries any 4xx, even when the server claims it is retryable', () => {
    for (const status of [400, 401, 403, 404, 409, 422, 429] as const) {
      expect(isRetryable(problem(status, true))).toBe(false);
    }
  });

  it('caps at MAX_RETRIES attempts and rejects contract failures outright', () => {
    expect(retryPolicy(3, new ApiError(problem(500, true)))).toBe(false);
    expect(retryPolicy(0, new ApiError(problem(500, true)))).toBe(true);
    expect(
      retryPolicy(
        0,
        new ApiError({
          kind: 'contract',
          class: 'contract',
          message: 'x',
          path: '/api/x',
          status: 200,
          run_id: null,
          retry_after_ms: null,
        }),
      ),
    ).toBe(false);
  });

  it('backoff is exponential and jittered into [base/2, base], not a fixed wait', () => {
    const failure = problem(502, true);
    const first = backoffMs(0, failure);
    expect(first).toBeGreaterThanOrEqual(125);
    expect(first).toBeLessThanOrEqual(250);
    const third = backoffMs(2, failure);
    expect(third).toBeLessThanOrEqual(1000);
  });
});

describe('the envelope doctrine', () => {
  it('accepts exactly { data, meta } and nothing else', () => {
    expect(() => unwrap({ data: {}, meta: null }, DashboardDecoder, '/api/dashboard')).toThrowError();
    // A server that grows a `success` key fails here, at the one unwrap point.
    const error = captureThrow(() => unwrap({ data: {}, meta: {}, success: true }, DashboardDecoder, '/api/dashboard'));
    expect(error).toBeInstanceOf(ApiError);
    const failure = (error as ApiError).failure;
    expect(failure.kind).toBe('contract');
    // `message` exists only on the contract and network arms of the typed failure; a
    // problem-arm failure carries RFC 7807 `detail` instead. Narrowing here is the
    // point -- a `success` key must be classified as a contract breach, not a 5xx.
    if (failure.kind === 'contract') {
      expect(failure.message).toContain('envelope keys were [data, meta, success], expected [data, meta]');
      expect(failure.path).toBe('/api/dashboard');
    }
  });

  it('a payload that fails its decoder is a contract failure naming the path', () => {
    const error = captureThrow(() =>
      unwrap({ data: { expected_loss_avoided: null }, meta: {} }, DashboardDecoder, '/api/dashboard'),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).failure.kind).toBe('contract');
  });
});
