/* FIXTURE TRANSPORT — developer contract double. See common.fixture.ts for the
   rule that keeps this out of a production build.

   It answers the same paths the real API answers, with the same envelope, so
   swapping it for P7's server is an environment variable and nothing else. Every
   response it produces carries `provenance: 'fixture:developer-contract'`, which
   the app shell prints as a banner: a fixture can be seen, never mistaken. */

import type { AlertPage } from '../lib/api/contract';
import type { TransportRequest, TransportResponse } from '../lib/api/transport';
import { alertPage, largeAlertPage } from './alerts.fixture';
import { CASES } from './case.fixture';
import { envelope } from './common.fixture';
import { dashboard } from './dashboard.fixture';
import { stressSubgraph, traversedSubgraph } from './graph.fixture';
import { datasetCard, datasetCardPaysim, runtime, stageEvents, validation } from './meta.fixture';
import { type AllocateParams, allocate, policyDefaults } from './policy.fixture';
import { disagreement, disagreementEmpty, drift, scorecard } from './scorecard.fixture';

function json(status: number, body: unknown): TransportResponse {
  return { status, contentType: 'application/json', body, retryAfterMs: null };
}

function problem(
  status: number,
  title: string,
  detail: string,
  runId: string | null,
  extra: Record<string, unknown> = {},
): TransportResponse {
  return {
    status,
    contentType: 'application/problem+json',
    body: {
      type: `https://oxbow.dev/problems/${title.toLowerCase().replace(/[^a-z]+/g, '-')}`,
      title,
      status,
      detail,
      instance: '/api/…',
      run_id: runId,
      trace_id: '01J4Z7TRACE00000000000000',
      retryable: status >= 500,
      ...extra,
    },
    retryAfterMs: null,
  };
}

function paramsOf(path: string): URLSearchParams {
  const query = path.includes('?') ? path.slice(path.indexOf('?') + 1) : '';
  return new URLSearchParams(query);
}

function numberParam(params: URLSearchParams, key: string, fallback: number): number {
  const raw = params.get(key);
  if (raw === null) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** The queue, filtered the way the server filters it: bands, typology, text. */
function filteredAlerts(params: URLSearchParams): AlertPage {
  const bands = (params.get('band') ?? '').split(',').filter((value) => value.length > 0);
  const typology = params.get('typology') ?? '';
  const search = (params.get('q') ?? '').toUpperCase();
  const rows = alertPage.rows.filter((row) => {
    if (bands.length > 0 && !bands.includes(row.band)) return false;
    if (typology.length > 0 && row.typology !== typology) return false;
    if (search.length > 0 && !row.account_key.includes(search)) return false;
    return true;
  });
  const emptied = rows.length === 0 && (bands.length > 0 || typology.length > 0 || search.length > 0);
  return {
    ...alertPage,
    rows,
    filter_recovery: emptied
      ? {
          narrowest: typology.length > 0 ? 'typology' : bands.length > 0 ? 'band' : 'q',
          label:
            typology.length > 0
              ? `Typology is ${typology}`
              : bands.length > 0
                ? `Band is ${bands.join(' or ')}`
                : `Text search “${search}”`,
          rows_if_removed:
            typology.length > 0
              ? alertPage.rows.length - rows.filter((r) => r.typology === null).length
              : alertPage.rows.length,
          unfiltered_rows: alertPage.rows.length,
        }
      : null,
  };
}

export function send(request: TransportRequest): Promise<TransportResponse> {
  const { path, method, body } = request;
  const params = paramsOf(path);
  const route = path.split('?')[0] ?? path;

  if (method === 'POST' && route.startsWith('/api/cases/')) {
    return Promise.resolve(decide(route, body));
  }
  if (route === '/api/policy/allocate') {
    const allocation = allocate(
      {
        capacity_minutes: numberParam(params, 'capacity_minutes', 12_000),
        recovery_rate: Number(params.get('recovery_rate') ?? '0.35'),
        analyst_cost_per_hour: numberParam(params, 'analyst_cost_per_hour', 900_000),
        friction_cost: numberParam(params, 'friction_cost', 2_500_000),
      } satisfies AllocateParams,
      params.get('allocator') === 'cpsat',
    );
    return Promise.resolve(json(200, envelope(allocation)));
  }
  if (route === '/api/policy') return Promise.resolve(json(200, envelope(policyDefaults)));
  if (route === '/api/dashboard') return Promise.resolve(json(200, envelope(dashboard)));
  if (route === '/api/meta/run') return Promise.resolve(json(200, envelope(runtime)));
  if (route === '/api/meta/dataset') {
    return Promise.resolve(json(200, envelope(params.get('corpus') === 'paysim' ? datasetCardPaysim : datasetCard)));
  }
  if (route === '/api/validation') return Promise.resolve(json(200, envelope(validation)));
  if (route === '/api/scorecard') return Promise.resolve(json(200, envelope(scorecard)));
  if (route === '/api/scorecard/drift') return Promise.resolve(json(200, envelope(drift)));
  if (route === '/api/scorecard/disagreement') {
    return Promise.resolve(json(200, envelope(params.get('empty') === '1' ? disagreementEmpty : disagreement)));
  }
  if (route === '/api/graph/subgraph') {
    const nodes = numberParam(params, 'nodes', 0);
    if (nodes > 0) return Promise.resolve(json(200, envelope(stressSubgraph(nodes))));
    // The double traverses, because the explorer publishes `account` and `hops` into
    // the query and the states behind them have to be reachable in dev. Ignoring them
    // left the hop slider inert and made the empty-window and no-cycles states
    // unaddressable anywhere except the gallery.
    return Promise.resolve(
      json(200, envelope(traversedSubgraph(params.get('account') ?? '', numberParam(params, 'hops', 2)))),
    );
  }
  if (route === '/api/alerts') {
    const limit = numberParam(params, 'limit', 40);
    if (numberParam(params, 'total', 0) === 10_000) {
      return Promise.resolve(
        json(200, envelope(largeAlertPage(limit, numberParam(params, 'offset', 0)), { limit, total: 10_000 })),
      );
    }
    const page = filteredAlerts(params);
    return Promise.resolve(json(200, envelope(page, { limit, total: page.rows.length, sort: 'rank', order: 'asc' })));
  }
  if (route.startsWith('/api/cases/')) {
    const key = decodeURIComponent(route.slice('/api/cases/'.length));
    const record = CASES[key];
    if (record === undefined) {
      return Promise.resolve(
        problem(404, 'Not found', `no case recorded for ${key} under this run`, '01J4Z7M2QK9N7V1C4X6E8G0B2D'),
      );
    }
    return Promise.resolve(json(200, envelope(record)));
  }
  if (route.startsWith('/api/runs/') && route.endsWith('/events')) {
    const failed = params.get('fail') === '1' ? 3 : null;
    return Promise.resolve(json(200, envelope(stageEvents(failed === null ? 6 : 5, failed))));
  }

  // A path the fixture does not implement is a 501 problem, never a silent empty
  // list: an empty list makes the screen show an empty state for a client gap.
  return Promise.resolve(problem(501, 'Not implemented', `the fixture transport has no route for ${route}`, null));
}

/* ------------------------------------------------------- decision writes -- */

type WriteBody = { decision: string; reason: string; expected_version: number; idempotency_key: string };

function decide(route: string, body: unknown): TransportResponse {
  const key = decodeURIComponent(route.slice('/api/cases/'.length));
  const record = CASES[key];
  if (record === undefined) {
    return problem(404, 'Not found', `no case recorded for ${key}`, '01J4Z7M2QK9N7V1C4X6E8G0B2D');
  }
  if (typeof body !== 'object' || body === null) {
    return problem(400, 'Bad request', 'the decision body is not an object', '01J4Z7M2QK9N7V1C4X6E8G0B2D');
  }
  const write = body as WriteBody;
  const reason = write.reason.trim();
  if (reason.length === 0) {
    // Server-side refusal of an empty reason, mirrored here so the UI's own block
    // is demonstrably not the only thing standing between an analyst and a
    // reasonless decision record.
    return problem(
      422,
      'Request could not be processed',
      'a written reason is required',
      '01J4Z7M2QK9N7V1C4X6E8G0B2D',
      {
        errors: [{ location: 'reason', message: 'must not be empty', value: write.reason }],
      },
    );
  }
  if (write.expected_version !== record.decision_version) {
    return problem(409, 'Conflict', 'another decision was recorded first', '01J4Z7M2QK9N7V1C4X6E8G0B2D', {
      current_version: record.decision_version,
      current_seq: record.decisions.length,
      decided_by: record.decisions.at(-1)?.actor ?? 'unknown',
      decided_at: record.decisions.at(-1)?.recorded_at ?? null,
    });
  }
  const seq = record.decisions.length + 1;
  const prev = record.decisions.at(-1)?.hash ?? null;
  const hash = `${seq.toString(16).repeat(4)}${(prev ?? 'genesis').slice(0, 60)}`;
  const decision = {
    seq,
    decision: write.decision as 'review' | 'escalate' | 'dismiss',
    reason,
    actor: 'analyst.okello',
    role: 'analyst',
    recorded_at: new Date(Date.UTC(2026, 8, 25, 9, 12, seq)).toISOString(),
    hash,
    prev_hash: prev,
    reversible_of: null,
    four_eyes_required: false,
    superseded_run: false,
  };
  record.decisions.push(decision);
  record.decision_version += 1;
  return json(201, envelope({ decision, version: record.decision_version, outbox_queued: true }));
}
