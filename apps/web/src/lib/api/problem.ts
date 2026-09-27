/* =============================================================================
   The error side of the client contract: RFC 9457 `application/problem+json`,
   as a typed discriminated union.

   Plan §13 makes this a P7 gate clause — "a typed error union rather than `any`"
   — and §19.2 makes `any` in `apps/web/src` a failing test. Both are satisfied by
   the same mechanism: the problem body is *decoded* (see ../codec.ts), so the
   failure branch has a real type, and the retry policy reads a property of that
   type rather than sniffing a message string.

   Discriminator: `status`, mapped onto `FailureClass`. RFC 9457 permits
   `about:blank` for `type`, so `type` cannot discriminate, and status is the one
   field every conformant server must send.

   Two non-negotiable behaviours live here:
   * 4xx is never retried (plan §18: "a retry loop around a 4xx is hammering a
     permanent failure"). The server's `retryable` flag can narrow that, never
     widen it — a 400 claiming `retryable: true` is a server bug, and the client
     refuses to be the thing that hammers the API on its behalf.
   * `run_id` survives to the render layer, because DESIGN.md §5 requires every
     error surface to print it with a copy button. A failure that loses its run
     id is unreportable, which is the same defect as a progress bar that lies.
   ============================================================================= */

import { type Decoder, array, boolean, integer, nullable, number, object, string } from '../codec';

/** A JSON scalar, which is what `ProblemFieldError.value` is on the server. */
export const scalarOrNull: Decoder<string | number | boolean | null> = {
  kind: 'scalar|null',
  decode(value, path) {
    if (value === null || value === undefined) return { ok: true, value: null };
    if (typeof value === 'string' || typeof value === 'boolean') return { ok: true, value };
    if (typeof value === 'number' && Number.isFinite(value)) return { ok: true, value };
    return { ok: false, error: { path, message: 'expected a JSON scalar or null' } };
  },
};

export type FieldError = { location: string; message: string; value: string | number | boolean | null };

const FieldErrorDecoder: Decoder<FieldError> = object('FieldError', {
  location: string,
  message: string,
  value: scalarOrNull,
});

/** The status codes P7 declares in `COMMON_ERROR_STATUSES`, plus 412/429 it names in `_titles`. */
export const PROBLEM_STATUSES = [400, 401, 403, 404, 409, 412, 422, 429, 500, 502, 503] as const;

export type ProblemStatus = (typeof PROBLEM_STATUSES)[number];

/**
 * The optimistic-concurrency extension on a 409 decision write (plan §13: "two
 * concurrent decision writes produce one success and one 409"). The UI offers a
 * merge view from these three fields and nothing else.
 */
export type VersionConflict = {
  current_version: number;
  current_seq: number | null;
  decided_by: string | null;
  decided_at: string | null;
};

const VersionConflictDecoder: Decoder<VersionConflict> = object('VersionConflict', {
  current_version: integer,
  current_seq: nullable(integer),
  decided_by: nullable(string),
  decided_at: nullable(string),
});

/**
 * `problem+json` as the server sends it. `status` is validated against the
 * declared set: an undocumented code is a contract failure, not a 500-shaped
 * guess, and the tier it renders at depends on knowing which it is.
 */
export interface ProblemDetail {
  type: string;
  title: string;
  status: ProblemStatus;
  detail: string | null;
  instance: string | null;
  run_id: string | null;
  trace_id: string | null;
  errors: FieldError[] | null;
  retryable: boolean;
  /** Present on a 409 decision write only. */
  conflict: VersionConflict | null;
}

export const ProblemDetailDecoder: Decoder<ProblemDetail> = {
  kind: 'ProblemDetail',
  decode(value, path) {
    const base: Decoder<Omit<ProblemDetail, 'status' | 'conflict'>> = object('ProblemDetailBase', {
      type: string,
      title: string,
      detail: nullable(string),
      instance: nullable(string),
      run_id: nullable(string),
      trace_id: nullable(string),
      errors: nullable(array(FieldErrorDecoder)),
      retryable: boolean,
    });
    const parsed = base.decode(value, path);
    if (!parsed.ok) return parsed;

    const status = number.decode((value as Record<string, unknown>).status ?? null, `${path}.status`);
    if (!status.ok) return status;
    const known = PROBLEM_STATUSES.find((candidate) => candidate === status.value);
    if (known === undefined) {
      return { ok: false, error: { path: `${path}.status`, message: `undocumented status ${String(status.value)}` } };
    }

    const raw = value as Record<string, unknown>;
    const hasConflict = raw.current_version !== undefined && raw.current_version !== null;
    if (!hasConflict) return { ok: true, value: { ...parsed.value, status: known, conflict: null } };
    const conflict = VersionConflictDecoder.decode(raw, path);
    return conflict.ok ? { ok: true, value: { ...parsed.value, status: known, conflict: conflict.value } } : conflict;
  },
};

/* ------------------------------------------------------------------------- */

/**
 * How a failure is handled. The error tier and the retry policy both switch on
 * this, so "what happens on a 409" is answerable from one type.
 */
export type FailureClass =
  /** 400/422 — the request is wrong; render against the field. */
  | 'validation'
  /** 401/403 — not entitled; a re-fetch cannot help. */
  | 'unauthorized'
  /** 404/412 — the resource or a precondition is gone. */
  | 'missing'
  /** 409 — optimistic-concurrency conflict; carries the current chain version. */
  | 'conflict'
  /** 429 — the server asked for a wait; surfaced, never silently hammered. */
  | 'rate-limited'
  /** 5xx — the server or one of its ports failed; retry with exponential backoff. */
  | 'server'
  /** A body that is not the declared shape: contract drift, not a user error. */
  | 'contract'
  /** Never reached the API at all. */
  | 'network';

export type ApiFailure =
  | { kind: 'problem'; class: 'validation'; problem: ProblemDetail; retry_after_ms: number | null }
  | { kind: 'problem'; class: 'unauthorized'; problem: ProblemDetail; retry_after_ms: number | null }
  | { kind: 'problem'; class: 'missing'; problem: ProblemDetail; retry_after_ms: number | null }
  | { kind: 'problem'; class: 'conflict'; problem: ProblemDetail; retry_after_ms: number | null }
  | { kind: 'problem'; class: 'rate-limited'; problem: ProblemDetail; retry_after_ms: number | null }
  | { kind: 'problem'; class: 'server'; problem: ProblemDetail; retry_after_ms: number | null }
  | {
      kind: 'contract';
      class: 'contract';
      message: string;
      path: string;
      status: number | null;
      run_id: string | null;
      retry_after_ms: number | null;
    }
  | { kind: 'network'; class: 'network'; message: string; status: null; run_id: null; retry_after_ms: null };

export function classifyStatus(status: number): Exclude<FailureClass, 'contract' | 'network'> {
  if (status === 400 || status === 422) return 'validation';
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 404 || status === 412) return 'missing';
  if (status === 409) return 'conflict';
  if (status === 429) return 'rate-limited';
  if (status >= 500) return 'server';
  return 'missing';
}

/**
 * THE retry rule, in one place; TanStack Query's `retry` option calls this and
 * nothing else decides. 5xx and the network path retry with exponential backoff,
 * every 4xx returns false permanently — including a 429, whose `Retry-After` is
 * surfaced to the analyst rather than spent as a silent hammering loop.
 */
export function isRetryable(failure: ApiFailure): boolean {
  if (failure.kind === 'network') return true;
  if (failure.kind === 'contract') return false;
  if (failure.problem.status < 500) return false;
  return failure.problem.retryable;
}

/** The run id wherever it survived, for the copy button on every error surface. */
export function failureRunId(failure: ApiFailure): string | null {
  if (failure.kind === 'problem') return failure.problem.run_id;
  if (failure.kind === 'contract') return failure.run_id;
  return null;
}

export function failureTitle(failure: ApiFailure): string {
  if (failure.kind === 'problem') return failure.problem.title;
  if (failure.kind === 'contract') return 'Response did not match the API contract';
  return 'The API could not be reached';
}

export function failureDetail(failure: ApiFailure): string | null {
  if (failure.kind === 'problem') return failure.problem.detail ?? failure.problem.instance;
  if (failure.kind === 'contract') return `${failure.message} at ${failure.path}`;
  return failure.message;
}

/** HTTP status where one exists, for the "attempt N · HTTP 503" line. */
export function failureStatus(failure: ApiFailure): number | null {
  return failure.kind === 'problem' ? failure.problem.status : null;
}

/** Field-level errors, for the inline tier. Non-null only on a 400/422. */
export function failureFields(failure: ApiFailure): FieldError[] {
  return failure.kind === 'problem' && failure.problem.errors !== null ? failure.problem.errors : [];
}

/** True when a pane should render the labelled degraded banner rather than fail. */
export function isDegraded(failure: ApiFailure): boolean {
  return failure.kind === 'problem' && (failure.problem.status === 502 || failure.problem.status === 503);
}

/**
 * True when the API's typed refusal is "this install has no completed run yet" —
 * the fresh-install case, which is an EMPTY state, not an error state. P7 answers
 * `404 https://oxbow.dev/problems/run-not-found` for every run-scoped route before
 * the first pipeline completes; plan §14 says a screen in that position must show
 * the no-run empty state (literal command, copy button, expected runtime), not a
 * retry invitation, because retrying cannot succeed until the pipeline has run.
 */
export function isRunNotFound(failure: ApiFailure): boolean {
  return failure.kind === 'problem' && failure.problem.status === 404 && failure.problem.type.includes('run-not-found');
}

/** Thrown by the transport so the failure reaches Query as a typed error. */
export class ApiError extends Error {
  readonly failure: ApiFailure;

  constructor(failure: ApiFailure) {
    super(failureTitle(failure));
    this.name = 'ApiError';
    this.failure = failure;
  }
}

/** Narrows a caught value to the typed failure, so no component casts. */
export function toApiFailure(error: unknown): ApiFailure | null {
  return error instanceof ApiError ? error.failure : null;
}
