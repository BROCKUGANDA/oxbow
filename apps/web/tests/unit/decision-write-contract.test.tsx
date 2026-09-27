/**
 * H14 — the four-eyes decision flow, from the UI to the route the API actually exposes.
 *
 * The claim under test is DESIGN.md §7: no route may render a value that is not in an
 * API response. On the decision write that meant four separate breaks stacked on top of
 * each other, any one of which was enough on its own: the page posted to
 * `/api/cases/{account_key}` where the router registers
 * `/api/cases/{case_id}/decisions` with `Path(min_length=26, max_length=26)`; it sent
 * `decision` where `DecisionCreate` declares `action`; it sent `idempotency_key`, which
 * `extra="forbid"` turns into a 422; and it decoded a receipt of
 * `{decision, version, outbox_queued}`, three names `DecisionWriteResult` has never
 * used. A fifth, found while writing this: the rail's own pane passed `meta={null}`
 * together with a skeleton spec, which is the pane's condition for "still loading", so
 * the decision form was never in the DOM at all.
 *
 * Every response below is either a byte-for-byte server shape
 * (`src/fixtures/server-shape.fixture.ts`) or a refusal produced by rules copied out of
 * `apps/api`, and the field lists are re-read from the Python source at test time so the
 * fixture goes visibly stale rather than quietly wrong when the server moves.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it, vi } from 'vitest';

import CasePage from '@/app/(dash)/cases/[id]/page';
import {
  DECISION_ACTIONS,
  SERVER_CASE_ID,
  SERVER_ROUTES,
  decisionCreate,
  decisionWriteResult,
  serverEnvelope,
  storedDecisionCurrent,
  validationBundle,
  versionConflict,
} from '@/fixtures/server-shape.fixture';
import { send as fixtureSend } from '@/fixtures/transport.fixture';
import { CasePayloadDecoder, DecisionWriteResultDecoder, ROUTES } from '@/lib/api/contract';
import { ProblemDetailDecoder } from '@/lib/api/problem';
import { mustFind, mustFindAll, text } from '@/test/render';
import {
  buttonByText,
  fixtureRouter,
  json,
  mountWithApi,
  press,
  problem,
  settle,
  teardown,
  typeInto,
} from './page-harness';

// The case route is a dynamic segment, so the page reads its key from the router.
vi.mock('next/navigation', () => ({
  useParams: () => ({ id: '01J4Z7M2QK9N7V1C4X6E8G0B2E' }),
  useSearchParams: () => new URLSearchParams(''),
}));

const CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2E';
const REASON_BOX = 'textarea[aria-label="Written reason for the decision"]';
const NOTE_BOX = 'textarea[aria-label="Confirmation note for the second reviewer"]';

let mounted: ReturnType<typeof mountWithApi> | null = null;
afterEach(() => {
  if (mounted !== null) teardown(mounted);
  mounted = null;
});

/* ------------------------------------------------ the Python source, re-read -- */

function apiSchema(): string {
  for (const relative of ['../../../../apps/api/schemas/case.py', '../../../../../apps/api/schemas/case.py']) {
    try {
      return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');
    } catch {
      // try the next candidate root
    }
  }
  throw new Error('apps/api/schemas/case.py is not readable from the web test root');
}

type PyField = { name: string; required: boolean };

/** The declared fields of a Pydantic model, in source order, with whether the server
 *  gives them a default (`= None` on `audit_seq` and `reversal_of_decision_id`, which
 *  means a response without them is conformant and the client may not require them).
 *  Stops at the first validator, method or nested class; `model_config` is an
 *  assignment, so it is not picked up as a field. */
function pydanticFields(source: string, className: string): PyField[] {
  const start = source.search(new RegExp(`^class ${className}\\(`, 'm'));
  if (start < 0) throw new Error(`no class ${className} in apps/api/schemas/case.py`);
  const fields: PyField[] = [];
  for (const line of source.slice(start).split('\n').slice(1)) {
    if (/^(class |@| {4}@| {4}def |__all__)/.test(line)) break;
    const match = /^ {4}([a-z_][a-z0-9_]*)\s*:(.+)$/.exec(line);
    if (match !== null) fields.push({ name: match[1] as string, required: !match[2]?.includes('=') });
  }
  return fields;
}

const fieldNames = (fields: PyField[]): string[] => fields.map((field) => field.name);

/** Write the reason, press one of the three actions, and let the write settle. */
async function decide(label: string, reason: string): Promise<void> {
  if (mounted === null) throw new Error('nothing mounted');
  await typeInto(mustFind<HTMLElement>(mounted.container, REASON_BOX), reason);
  await press(mounted.container, buttonByText(mounted.container, label));
}

describe('the server shape the client codes against', () => {
  it('names exactly the fields apps/api/schemas/case.py declares', () => {
    const source = apiSchema();
    expect(fieldNames(pydanticFields(source, 'DecisionWriteResult'))).toEqual([
      'case_id',
      'decision_id',
      'decision_seq',
      'chain_seq',
      'row_hash',
      'four_eyes_required',
      'four_eyes_state',
      'outbox_queued',
      'case_version',
      'decided_on_superseded_run',
      'audit_seq',
      'occurred_at',
    ]);
    expect(fieldNames(pydanticFields(source, 'DecisionCreate'))).toEqual([
      'action',
      'reason',
      'expected_version',
      'reversal_of_decision_id',
    ]);
    // Only `audit_seq` is defaulted on the receipt, so only it may be absent from a 200.
    expect(
      pydanticFields(source, 'DecisionWriteResult')
        .filter((field) => !field.required)
        .map((f) => f.name),
    ).toEqual(['audit_seq']);
  });

  it('decodes, and requires every field the server does not default', () => {
    const sample = decisionWriteResult();
    expect(DecisionWriteResultDecoder.decode(sample, '').ok).toBe(true);
    const fields = pydanticFields(apiSchema(), 'DecisionWriteResult');
    // Bidirectional. A decoder that silently skipped a server field passes the positive
    // case above and fails here, once per field the server requires.
    for (const field of fields) {
      const stripped: Record<string, unknown> = { ...sample };
      delete stripped[field.name];
      const result = DecisionWriteResultDecoder.decode(stripped, '');
      if (field.required) {
        expect(result.ok, `the receipt decoder accepted a response without ${field.name}`).toBe(false);
        if (!result.ok) expect(result.error.path).toBe(field.name);
      } else {
        expect(result.ok, `${field.name} has a server-side default, so it must be optional`).toBe(true);
      }
    }
  });

  it('accepts the version-conflict document apps/api/problems.py actually emits', () => {
    const decoded = ProblemDetailDecoder.decode(versionConflict(), '');
    expect(decoded.ok).toBe(true);
    if (!decoded.ok) return;
    expect(decoded.value.status).toBe(409);
    expect(decoded.value.conflict?.current_version).toBe(4);
    expect(decoded.value.conflict?.expected_version).toBe(3);
    // The merge view is built from the winning row, so it has to survive the decoder.
    expect(decoded.value.conflict?.current?.actor_id).toBe('reviewer.nabirye');
    expect(decoded.value.conflict?.current?.reason).toContain('agent network');
  });
});

describe('the fixture double refuses what the API refuses', () => {
  it('rejects an account key on a route whose path parameter is 26 characters', async () => {
    const response = await fixtureSend({
      path: '/api/cases/ACC-7F2A19/decisions',
      method: 'POST',
      body: decisionCreate(),
    });
    expect(response.status).toBe(422);
  });

  it("rejects the old body: `decision` and `idempotency_key` are not DecisionCreate's fields", async () => {
    const response = await fixtureSend({
      path: SERVER_ROUTES.createDecision(CASE_ID),
      method: 'POST',
      body: {
        decision: 'escalate',
        reason: 'Escalating with the loop attached.',
        expected_version: 3,
        idempotency_key: 'run:account:4',
      },
    });
    expect(response.status).toBe(422);
    const errors = (response.body as { errors?: { location: string }[] }).errors ?? [];
    // Three, not two. `DecisionCreate` is `extra="forbid"`, so the stale body is wrong in
    // three distinct ways: `action` is missing, and BOTH `decision` and `idempotency_key`
    // are keys the server has never heard of. The list that was here contradicted the title
    // of the test right above it.
    expect(errors.map((entry) => entry.location).sort()).toEqual(['action', 'decision', 'idempotency_key']);
  });

  it('answers the body the contract now sends with a DecisionWriteResult envelope', async () => {
    const response = await fixtureSend({
      path: SERVER_ROUTES.createDecision(CASE_ID),
      method: 'POST',
      body: decisionCreate(),
    });
    expect(response.status).toBe(200);
    const envelope = response.body as { data: Record<string, unknown>; meta: Record<string, unknown> };
    expect(Object.keys(envelope).sort()).toEqual(['data', 'meta']);
    expect(DecisionWriteResultDecoder.decode(envelope.data, '').ok).toBe(true);
  });
});

describe('the decision rail, driven from the page', () => {
  it('renders the rail at all: it used to sit behind its own permanent skeleton', async () => {
    mounted = mountWithApi(<CasePage />, fixtureRouter);
    await settle();
    expect(text(mounted.container)).not.toContain('Decision is loading');
    expect(text(mounted.container)).toContain('Written reason — required');
  });

  it('posts to /api/cases/{case_id}/decisions with DecisionCreate’s own field names', async () => {
    let written: unknown = null;
    mounted = mountWithApi(<CasePage />, (request) => {
      if (request.method === 'POST' && request.path.endsWith('/decisions')) {
        written = request.body;
        return Promise.resolve(json(200, serverEnvelope(decisionWriteResult())));
      }
      return fixtureSend(request);
    });
    await settle();
    await decide('Escalate', 'Third cycle member identified; escalating with the loop attached.');

    expect(written).not.toBeNull();
    const body = written as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(['action', 'expected_version', 'reason', 'reversal_of_decision_id']);
    expect(DECISION_ACTIONS).toContain(body.action);
    expect(body.action).toBe('escalate');
    expect(typeof body.expected_version).toBe('number');
    // The route the router registers, keyed on the 26-character id its path demands.
    expect(mounted.lastRequestTo('/decisions')?.path).toBe(`/api/cases/${CASE_ID}/decisions`);
  });

  it('renders the four-eyes hold from the receipt and offers its second reviewer', async () => {
    mounted = mountWithApi(<CasePage />, (request) => {
      if (request.method === 'POST' && request.path.endsWith('/decisions')) {
        return Promise.resolve(json(200, serverEnvelope(decisionWriteResult({ outbox_queued: false }))));
      }
      if (request.method === 'POST' && request.path.endsWith('/confirm')) {
        return Promise.resolve(
          json(200, serverEnvelope(decisionWriteResult({ four_eyes_state: 'confirmed', outbox_queued: true }))),
        );
      }
      return fixtureSend(request);
    });
    await settle();
    await decide('Escalate', 'Escalating: the loop closes inside the window.');

    // Every fact below came back in the receipt: the decision number, the case it landed
    // on, and the chain hash of the audit row.
    const rail = text(mounted.container);
    expect(rail).toContain('Decision #3');
    expect(rail).toContain(SERVER_CASE_ID);
    expect(rail).toContain('31b7e0c4d9a2f6510c');
    const hold = mustFind(mounted.container, '[data-four-eyes]');
    expect(hold.textContent).toContain('Held for a second reviewer');
    expect(hold.textContent).toContain('delivery not queued');

    const note = mustFind<HTMLElement>(mounted.container, NOTE_BOX);
    await typeInto(note, 'Read the loop; second reviewer concurs.');
    await press(mounted.container, buttonByText(mounted.container, 'Confirm as second reviewer'));

    expect(mounted.lastRequestTo('/confirm')?.path).toBe('/api/decisions/01J4Z7M2QK9N7V1C4X6E8G0B2ED1/confirm');
    expect(Object.keys((mounted.lastRequestTo('/confirm')?.body ?? {}) as Record<string, unknown>).sort()).toEqual([
      'confirmation_note',
      'expected_version',
    ]);
    // Confirmed means the hold is gone — it is a state of the receipt, not a guess.
    expect(text(mounted.container)).not.toContain('Held for a second reviewer');
  });

  it('renders the merge view from the row the 409 carried, not from blanks', async () => {
    const conflict = versionConflict(storedDecisionCurrent());
    mounted = mountWithApi(<CasePage />, (request) => {
      if (request.method === 'POST' && request.path.endsWith('/decisions')) {
        return Promise.resolve(
          problem(409, conflict.type, conflict.title, String(conflict.detail), {
            expected_version: conflict.expected_version,
            current_version: conflict.current_version,
            current: conflict.current,
          }),
        );
      }
      return fixtureSend(request);
    });
    await settle();
    await decide('Dismiss', 'Closing it — the counterparties are the same rail agent.');

    const alerts = mustFindAll(mounted.container, '[role="alert"]');
    expect(alerts.length).toBeGreaterThan(0);
    const merge = mustFind(mounted.container, '[data-merge-view]');
    expect(merge.textContent).toContain('dismiss');
    expect(merge.textContent).toContain('reviewer.nabirye');
    expect(merge.textContent).toContain('agent network');
    expect(merge.textContent).toContain('retry against version 4');
    // The §7 version of this claim: nothing on the surface may be a field the 409
    // omitted. `—` in the actor slot was exactly that.
    expect(merge.textContent).not.toContain('# by');
  });
});

/* ------------------------------------------------------------- tripwires --- */

describe('known, unfixed divergences named so the suite sees them', () => {
  it('the case READ is still unreconciled: a real CaseDetail does not decode as CasePayload', () => {
    // apps/api answers GET /api/cases/{case_id} with `CaseDetail` (schemas/case.py:197-234)
    // — `pinned_run_id`, `fused_score`, `scorecard_points`, `decision_history`. The client
    // decodes a nested `CasePayload` (`header`, `contributions`, `decision_version`). The
    // two shapes share no field, so the workspace could only ever have rendered from a
    // double that agreed with itself. Not worked around here — that is the next pass —
    // but it must not be mistaken for a seam that is already closed.
    const detail: Record<string, unknown> = {
      case_id: CASE_ID,
      pinned_run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
      run_state: 'complete',
      superseded: false,
      account_key: 'ACC-7F2A19',
      band: 'E',
      fused_score: 0.912,
      scorecard_points_total: 71,
      scorecard_points: [],
      calibration: { band: 'E', observed_rate: 0.71, n: 432, note: null },
      predicted_typology: 'R4',
      model_version: 'lgbm-fusion-4.5.3+iso',
      reason_codes: [],
      rule_ids: [],
      economics: {},
      evidence: [],
      transactions: [],
      transaction_total: 0,
      shap: [],
      rule_hits: [],
      decision_history: [],
      case_version: 3,
      status: 'open',
      rank_under_active_policy: 4,
      counterfactual: null,
    };
    const result = CasePayloadDecoder.decode(detail, '');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.path).toBe('header');
  });

  it('the server answers /api/validation with ValidationBundle, which the page refuses', () => {
    const decoded = ROUTES.validation.data.decode(validationBundle(), '');
    expect(decoded.ok).toBe(false);
  });
});
