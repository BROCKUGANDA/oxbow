/* =============================================================================
   Failure-injection transport, for `/dev/states` only.

   The four error tiers and the degraded banner are states the product must handle,
   and they are the states nobody designs. Demonstrating them needs a server that
   fails on demand; taking P7 down is not something a demo machine can do reliably,
   so this transport answers a `fail=` parameter with a real RFC 9457 document and
   otherwise defers to the fixture transport.

   It is not a data source. Every success path passes through unchanged, and every
   failure path produces a problem body carrying a run id, which is what the panes
   then render. Two of the injected cases are deliberate contract violations — a
   200 whose envelope is wrong, and a 200 whose payload is missing fields — because
   the contract tier of the error ladder is the one nobody can otherwise reach.
   ============================================================================= */

import type { TransportRequest, TransportResponse } from './transport';
import { send as fixtureSend } from '../../fixtures/transport.fixture';

function problem(
  status: number,
  title: string,
  detail: string,
  extra: Record<string, unknown> = {},
): TransportResponse {
  return {
    status,
    contentType: 'application/problem+json',
    retryAfterMs: null,
    body: {
      type: `https://oxbow.dev/problems/${title.toLowerCase().replace(/[^a-z]+/g, '-')}`,
      title,
      status,
      detail,
      instance: '/api/…',
      run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
      trace_id: '01J4Z7TRACE00000000000000',
      retryable: status >= 500,
      ...extra,
    },
  };
}

/** A 503 naming its dependency and its fallback, so the degraded banner renders
 *  from the response rather than from a client-side guess about what is down. */
function unavailable(dependency: string, fallback: string): TransportResponse {
  return problem(503, 'Dependency unavailable', `${dependency} is not answering on this deployment`, {
    errors: [{ location: 'dependency', message: fallback, value: dependency }],
  });
}

export function send(request: TransportRequest): Promise<TransportResponse> {
  const query = request.path.includes('?') ? request.path.slice(request.path.indexOf('?') + 1) : '';
  switch (new URLSearchParams(query).get('fail')) {
    case '500':
      return Promise.resolve(problem(500, 'Internal error', 'the router raised; the run id above names the log line'));
    case '502':
      return Promise.resolve(problem(502, 'Upstream failure', 'the warehouse port returned a reset mid-query'));
    case '503-solver':
      return Promise.resolve(unavailable('CP-SAT solver port', 'the greedy allocation by EV density'));
    case '503-summariser':
      return Promise.resolve(unavailable('narrative summariser', 'the deterministic template narrative'));
    case '503-mlflow':
      return Promise.resolve(unavailable('MLflow tracking store', 'the model version stamped on the run record'));
    case '409':
      return Promise.resolve(
        problem(409, 'Conflict', 'a decision was recorded on this case while you were writing yours', {
          current_version: 3,
          current_seq: 3,
          decided_by: 'reviewer.nabirye',
          decided_at: '2026-09-25T09:12:00Z',
        }),
      );
    case '404':
      return Promise.resolve(problem(404, 'Not found', 'no case recorded for that account under this run'));
    case '422':
      return Promise.resolve(
        problem(422, 'Request could not be processed', 'a written reason is required before a decision is recorded', {
          errors: [{ location: 'reason', message: 'must not be empty', value: '' }],
        }),
      );
    case 'broken-envelope':
      return Promise.resolve({
        status: 200,
        contentType: 'application/json',
        retryAfterMs: null,
        // The exact shape P7's doctrine forbids: a `success` key and no envelope.
        body: { data: [], success: true },
      });
    case 'broken-payload':
      return Promise.resolve({
        status: 200,
        contentType: 'application/json',
        retryAfterMs: null,
        // A 2xx whose `data` is missing every field the route declares.
        body: { data: { expected_loss_avoided: { value: {} } }, meta: {} },
      });
    case 'no-assumptions':
      /* A response that satisfies the envelope and the payload and strips exactly one
         thing: the assumptions block. The currency chokepoint refuses to render without
         it, so this is the case that proves the refusal is real — and on a screen whose
         money sits outside a pane, it is also the only way to reach tier 3, the
         route-level boundary, in a browser. */
      return fixtureSend(request).then((response) => ({
        ...response,
        body: stripAssumptions(response.body),
      }));
    default:
      return fixtureSend(request);
  }
}

/** Narrows an unknown response body to its envelope and empties `meta.assumptions`. */
function stripAssumptions(body: unknown): unknown {
  if (typeof body !== 'object' || body === null) return body;
  const envelope = body as Record<string, unknown>;
  const meta = envelope.meta;
  if (typeof meta !== 'object' || meta === null) return body;
  return { ...envelope, meta: { ...(meta as Record<string, unknown>), assumptions: [] } };
}
