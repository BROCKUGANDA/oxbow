/* FIXTURE TRANSPORT — developer contract double. See common.fixture.ts for the
   rule that keeps this out of a production build.

   It answers the same paths the real API answers, with the same envelope, so
   swapping it for P7's server is an environment variable and nothing else. Every
   response it produces carries `provenance: 'fixture:developer-contract'`, which
   the app shell prints as a banner: a fixture can be seen, never mistaken. */

import type { AlertPage, CasePayload, DecisionAction } from '../lib/api/contract';
import type { TransportRequest, TransportResponse } from '../lib/api/transport';
import { alertPage, largeAlertPage } from './alerts.fixture';
import { CASES, CASE_ID_BY_ACCOUNT_KEY } from './case.fixture';
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
  if (method === 'POST' && route.startsWith('/api/decisions/')) {
    return Promise.resolve(confirmFourEyes(route, body));
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
    const record = caseRecord(route.slice('/api/cases/'.length));
    if (record === null) {
      return Promise.resolve(
        problem(404, 'Not found', `no case recorded for ${decodeURIComponent(route.slice('/api/cases/'.length))}`, RUN),
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

/* ------------------------------------------------------- decision writes --

   These two handlers mirror `apps/api/routers/decisions.py` and the two Pydantic models
   it uses, rule for rule, so the fixture transport cannot keep a client bug alive. A
   wrong path, a wrong field name or an undeclared key is answered the way the API
   answers it — which is the whole point: the old double accepted `POST
   /api/cases/{account_key}` with `{decision, reason, expected_version,
   idempotency_key}` and returned `{decision, version, outbox_queued}`, none of which the
   server has ever sent, so the mismatch was invisible until someone put a real API
   behind the page. Field-for-field shapes and their source lines:
   `server-shape.fixture.ts`.                                                                  */

const RUN = '01J4Z7M2QK9N7V1C4X6E8G0B2D';
/** `Path(min_length=26, max_length=26)` — routers/decisions.py:54, cases.py:80. */
const CASE_ID_LENGTH = 26;
/** `REASON_MIN_CHARS` / `REASON_MAX_CHARS` — schemas/case.py:32-33. */
const REASON_MIN = 3;
const REASON_MAX = 4000;
/** `DecisionAction` — schemas/case.py:27. */
const ACTIONS = ['escalate', 'dismiss', 'review', 'reverse'];
/** `DecisionCreate.model_config = ConfigDict(extra="forbid")` — schemas/case.py:240.
 *  `reversal_of_decision_id` defaults to None there (`:250`), so it may be absent; every
 *  other declared field is required. */
const CREATE_KEYS = ['action', 'reason', 'expected_version', 'reversal_of_decision_id'];
const CREATE_REQUIRED = ['action', 'reason', 'expected_version'];
/** `FourEyesConfirm`, also `extra="forbid"` — schemas/case.py:293-296, both required. */
const CONFIRM_KEYS = ['expected_version', 'confirmation_note'];
const CONFIRM_REQUIRED = ['expected_version', 'confirmation_note'];
/** `four_eyes.threshold_exposure_minor` in config/economics.yaml, compared strictly
 *  (`apps/api/decisions.py:258`): above it a row is chained but nothing is queued. */
const FOUR_EYES_THRESHOLD_MINOR = 50_000_000;
/** A case opens at `version=1` and every write bumps it (apps/api/decisions.py:174,
 *  :342), so the fixture's `decision_version` mirrors the stored token. */

/** case_id → the same fixture record the account-key deep links resolve to. */
const CASES_BY_ID: Record<string, CasePayload> = {};
for (const [accountKey, caseId] of Object.entries(CASE_ID_BY_ACCOUNT_KEY)) {
  const record = CASES[accountKey];
  if (record !== undefined) CASES_BY_ID[caseId] = record;
}

/** decision_id → the case it was written on, so the confirm route finds its row the way
 *  the server looks one up (`routers/decisions.py:97-128`) rather than guessing at one. */
const CASE_BY_DECISION_ID = new Map<string, CasePayload>();

function caseIdOf(record: CasePayload): string {
  const entry = Object.entries(CASE_ID_BY_ACCOUNT_KEY).find(([key]) => CASES[key] === record);
  return entry?.[1] ?? '';
}

function decisionIdFor(record: CasePayload, seq: number): string {
  return `01J4Z7D${caseIdOf(record).slice(6, 15)}S${String(seq).padStart(10, '0')}`.slice(0, 64);
}

/** Look a fixture case up by `case_id`, or — for the queue's still-unreconciled
 *  `case_href`, which carries an account key — by account key. Null for anything else. */
function caseRecord(rawSegment: string): CasePayload | null {
  const key = decodeURIComponent(rawSegment);
  if (key.length === CASE_ID_LENGTH) return CASES_BY_ID[key] ?? null;
  return CASES[key] ?? null;
}

function unprocessable(detail: string, location: string, message: string, value: unknown): TransportResponse {
  return problem(422, 'Request could not be processed', detail, RUN, {
    errors: [{ location, message, value }],
  });
}

/** `RequestValidationError` for a path parameter outside its `Path(...)` bounds. */
function pathRejected(parameter: string, detail: string): TransportResponse {
  return unprocessable(
    `1 validation error: ${parameter} — ${detail}`,
    parameter,
    detail,
    `field ${parameter} fails Path(min_length=26, max_length=26)`,
  );
}

/** A result union rather than a type predicate: `TransportResponse` is a type alias of
 *  known properties, so it is assignable to `Record<string, unknown>` and a
 *  `value is Record<string, unknown>` guard would narrow nothing at all. */
type BodyCheck =
  | { readonly ok: true; readonly record: Record<string, unknown> }
  | { readonly ok: false; readonly response: TransportResponse };

/** The subset of `DecisionCreate`'s validation the client can get wrong, reported the way
 *  Pydantic reports it: every field error at once, each naming its own location. */
function validateBody(body: unknown, allowed: readonly string[], required: readonly string[]): BodyCheck {
  const fail = (detail: string, errors: { location: string; message: string; value: unknown }[]): BodyCheck => ({
    ok: false,
    response: problem(422, 'Request could not be processed', detail, RUN, { errors }),
  });
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return fail('the decision body is not an object', [
      { location: 'body', message: 'JSON object required', value: body ?? null },
    ]);
  }
  const record = body as Record<string, unknown>;
  const errors: { location: string; message: string; value: unknown }[] = [];
  for (const key of required) {
    if (!(key in record)) errors.push({ location: key, message: 'Field required', value: null });
  }
  for (const key of Object.keys(record)) {
    // `extra="forbid"`: an undeclared key is a 422 here, not an ignored hint.
    if (!allowed.includes(key))
      errors.push({ location: key, message: 'Extra inputs are not permitted', value: record[key] });
  }
  if (errors.length > 0) {
    return fail(
      `${String(errors.length)} validation error(s): ${errors.map((entry) => entry.location).join(', ')}`,
      errors,
    );
  }
  return { ok: true, record };
}

function decide(route: string, body: unknown): TransportResponse {
  const segments = route.slice('/api/cases/'.length).split('/');
  const rawCaseId = segments[0] ?? '';
  if (segments.length !== 2 || segments[1] !== 'decisions') {
    // No other POST lives under /api/cases/: `@router.post("/api/cases/{case_id}/decisions")`.
    return problem(404, 'Not found', `no route POST ${route}`, RUN);
  }
  const caseId = decodeURIComponent(rawCaseId);
  if (caseId.length !== CASE_ID_LENGTH) {
    return pathRejected('case_id', `string length must be between 26 and 26, got ${String(caseId.length)}`);
  }
  const record = caseRecord(rawCaseId);
  if (record === null) return problem(404, 'Not found', `no case ${caseId}`, RUN);

  const checked = validateBody(body, CREATE_KEYS, CREATE_REQUIRED);
  if (!checked.ok) return checked.response;
  const action = checked.record.action;
  const reason = checked.record.reason;
  const expectedVersion = checked.record.expected_version;
  if (typeof action !== 'string' || !ACTIONS.includes(action)) {
    return unprocessable('1 validation error: action', 'action', `input not in ${ACTIONS.join(', ')}`, action);
  }
  if (typeof reason !== 'string' || reason.trim().length < REASON_MIN || reason.length > REASON_MAX) {
    return unprocessable(
      'a decision reason cannot be blank or whitespace: it is the sentence that survives into the audit chain',
      'reason',
      `string length must be between ${String(REASON_MIN)} and ${String(REASON_MAX)}`,
      reason,
    );
  }
  if (typeof expectedVersion !== 'number' || !Number.isInteger(expectedVersion) || expectedVersion < 1) {
    return unprocessable(
      '1 validation error: expected_version',
      'expected_version',
      'Input should be greater than or equal to 1',
      expectedVersion,
    );
  }
  if (expectedVersion !== record.decision_version) {
    return problem(
      409,
      'Case changed since you loaded it',
      `case ${caseId} is at version ${String(record.decision_version)}; this write expected ${String(expectedVersion)}`,
      RUN,
      {
        expected_version: expectedVersion,
        current_version: record.decision_version,
        current: storedDecision(record),
      },
    );
  }
  return appendDecision(record, caseId, action, reason.trim());
}

/** The winning row, in exactly the shape `apps/api/decisions.py:803-813` embeds in the
 *  409: the merge view is built from these fields and nothing else. */
function storedDecision(record: CasePayload): Record<string, unknown> | null {
  const last = record.decisions.at(-1);
  if (last === undefined) return null;
  return {
    decision_id: decisionIdFor(record, last.seq),
    decision_seq: last.seq,
    action: last.decision,
    reason: last.reason,
    actor_id: last.actor,
    occurred_at: last.recorded_at,
    row_hash: last.hash,
    four_eyes_state: last.four_eyes_required ? 'pending' : 'not_required',
    status: 'open',
  };
}

/** The second reviewer's write — `POST /api/decisions/{decision_id}/confirm`. */
function confirmFourEyes(route: string, body: unknown): TransportResponse {
  const segments = route.slice('/api/decisions/'.length).split('/');
  const decisionId = decodeURIComponent(segments[0] ?? '');
  if (segments.length !== 2 || segments[1] !== 'confirm' || decisionId.length === 0) {
    return problem(404, 'Not found', `no route POST ${route}`, RUN);
  }
  const record = CASE_BY_DECISION_ID.get(decisionId);
  if (record === undefined) {
    return problem(404, 'Not found', `no decision ${decisionId} was written by this double`, RUN);
  }
  const checked = validateBody(body, CONFIRM_KEYS, CONFIRM_REQUIRED);
  if (!checked.ok) return checked.response;
  const note = checked.record.confirmation_note;
  const expectedVersion = checked.record.expected_version;
  if (typeof note !== 'string' || note.trim().length < REASON_MIN || note.length > REASON_MAX) {
    return unprocessable(
      '1 validation error: confirmation_note',
      'confirmation_note',
      `string length must be between ${String(REASON_MIN)} and ${String(REASON_MAX)}`,
      note,
    );
  }
  if (typeof expectedVersion !== 'number' || expectedVersion !== record.decision_version) {
    return problem(
      409,
      'Case changed since you loaded it',
      `the case moved to version ${String(record.decision_version)}`,
      RUN,
      {
        expected_version: expectedVersion,
        current_version: record.decision_version,
        current: storedDecision(record),
      },
    );
  }
  const last = record.decisions.at(-1);
  return json(
    200,
    envelope({
      case_id: caseIdOf(record),
      decision_id: decisionId,
      decision_seq: last?.seq ?? 0,
      chain_seq: last?.seq ?? 0,
      row_hash: last?.hash ?? 'genesis',
      four_eyes_required: true,
      four_eyes_state: 'confirmed',
      outbox_queued: true,
      case_version: record.decision_version,
      decided_on_superseded_run: false,
      audit_seq: last?.seq ?? 0,
      occurred_at: new Date(Date.UTC(2026, 8, 25, 10, 0, 0)).toISOString(),
    }),
  );
}

function appendDecision(record: CasePayload, caseId: string, action: string, reason: string): TransportResponse {
  const seq = record.decisions.length + 1;
  const prev = record.decisions.at(-1)?.hash ?? null;
  const hash = `${seq.toString(16).repeat(4)}${(prev ?? 'genesis').slice(0, 60)}`;
  const fourEyes = record.header.economics.exposure.value.minor > FOUR_EYES_THRESHOLD_MINOR;
  const decisionId = decisionIdFor(record, seq);
  record.decisions.push({
    seq,
    decision: action as DecisionAction,
    reason,
    actor: 'analyst.okello',
    role: 'analyst',
    recorded_at: new Date(Date.UTC(2026, 8, 25, 9, 12, seq)).toISOString(),
    hash,
    prev_hash: prev,
    reversible_of: null,
    four_eyes_required: fourEyes,
    superseded_run: false,
  });
  record.decision_version += 1;
  CASE_BY_DECISION_ID.set(decisionId, record);
  return json(
    200,
    envelope({
      case_id: caseId,
      decision_id: decisionId,
      decision_seq: seq,
      chain_seq: seq,
      row_hash: hash,
      four_eyes_required: fourEyes,
      four_eyes_state: fourEyes ? 'pending' : 'not_required',
      outbox_queued: !fourEyes,
      case_version: record.decision_version,
      decided_on_superseded_run: false,
      audit_seq: seq,
      occurred_at: new Date(Date.UTC(2026, 8, 25, 9, 12, seq)).toISOString(),
    }),
  );
}
