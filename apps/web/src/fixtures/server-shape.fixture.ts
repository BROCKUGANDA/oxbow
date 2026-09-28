/* =============================================================================
   SERVER-SHAPE FIXTURE — a hand-written mirror of what `apps/api` sends TODAY.

   WHY THIS EXISTS ALONGSIDE THE FIXTURE TRANSPORT. `transport.fixture.ts` is the
   *developer contract double*: it answers with the shapes `lib/api/contract.ts`
   declares, which is exactly how a client-side invention survives for a whole
   milestone. The decision write is the proof — the double happily accepted
   `POST /api/cases/{account_key}` with `{decision, reason, expected_version,
   idempotency_key}` and returned `{decision, version, outbox_queued}`, and every
   one of those six names is absent from the server. A second double that agrees
   with the client cannot catch a client that disagrees with the server, so this
   file mirrors the Pydantic models instead, field for field.

   SOURCES, AS THEY EXIST ON DISK AT THE TIME OF WRITING. Every object below cites
   the file and line range it was copied from:

     apps/api/schemas/common.py:31-45    AssumptionLine   -> serverAssumptionLine
     apps/api/schemas/common.py:47-69    Money            -> serverMoney
     apps/api/schemas/common.py:72-90    Meta             -> serverMeta
     apps/api/schemas/common.py:106-121  Envelope         -> serverEnvelope
     apps/api/schemas/case.py:27         DecisionAction   -> DECISION_ACTIONS
     apps/api/schemas/case.py:32-33      REASON_M*_CHARS  -> REASON_MIN / REASON_MAX
     apps/api/schemas/case.py:173-194    DecisionRecordRow-> decisionRecordRow
     apps/api/schemas/case.py:197-234    CaseDetail       -> CASE_DETAIL_FIELDS
     apps/api/schemas/case.py:237-265    DecisionCreate   -> decisionCreate
     apps/api/schemas/case.py:268-289    DecisionWriteResult -> decisionWriteResult
     apps/api/schemas/case.py:292-296    FourEyesConfirm  -> fourEyesConfirm
     apps/api/problems.py:66-105         ProblemDetail    -> problemDocument
     apps/api/problems.py:188-212        VersionConflict  -> versionConflict
     apps/api/decisions.py:803-813       the `current` dict  -> storedDecisionCurrent
     apps/api/routers/decisions.py:41-57 / 91-128  the two writes -> SERVER_ROUTES
     apps/api/routers/cases.py:73-85     GET /api/cases/{case_id}
     apps/api/schemas/validation.py:231-255 ValidationBundle -> validationBundle
     apps/api/schemas/validation.py:43-89  FoldRow        -> foldRow
     apps/api/schemas/catalog.py:238-249 NetworkNode      -> NETWORK_NODE_FIELDS

   KEEPING THIS HONEST. `tests/unit/decision-write-contract.test.ts` reads the two
   Pydantic model bodies out of `apps/api/schemas/case.py` at test time and fails if
   the field list below no longer matches them. So a server change makes this file
   visibly stale in the suite rather than silently wrong — which is the only property
   a mirror like this can have, given three agents are editing `apps/api` concurrently
   and its shape is moving under the client.

   Nothing here is a production module: it lives under `src/fixtures/**`, which
   `resolveMode` keeps out of a production build (see common.fixture.ts).
   ============================================================================= */

/** `Path(min_length=26, max_length=26)` — routers/decisions.py:54, cases.py:80, :204, :138. */
export const CASE_ID_PATH_LENGTH = 26;
/** `Path(min_length=1, max_length=64)` — routers/decisions.py:99. */
export const DECISION_ID_PATH_MAX_LENGTH = 64;
/** `DecisionAction` — schemas/case.py:27. */
export const DECISION_ACTIONS = ['escalate', 'dismiss', 'review', 'reverse'] as const;
export type ServerDecisionAction = (typeof DECISION_ACTIONS)[number];
/** `four_eyes_state` — schemas/case.py:188, :284. */
export const FOUR_EYES_STATES = ['not_required', 'pending', 'confirmed'] as const;
/** `REASON_MAX_CHARS` / `REASON_MIN_CHARS` — schemas/case.py:32-33. */
export const REASON_MIN = 3;
export const REASON_MAX = 4000;

/** The case_id used by every fixture route in this file: 26 characters, like the API's. */
export const SERVER_CASE_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2E';
export const SERVER_RUN_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2D';
export const SERVER_DECISION_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2ED1';

/** The two writes, exactly as the router registers them. */
export const SERVER_ROUTES = {
  createDecision: (caseId: string): string => `/api/cases/${caseId}/decisions`,
  confirmDecision: (decisionId: string): string => `/api/decisions/${decisionId}/confirm`,
  caseDetail: (caseId: string): string => `/api/cases/${caseId}`,
} as const;

/* ------------------------------------------------------------------ scalars */

/** `Money` — schemas/common.py:47-69. `decimals` is the EXPONENT whose base is config's
 *  `minor_units_per_major` (the field's own description says so at :58-64): 100 minor
 *  units per major unit is `decimals: 2`, never `decimals: 100`. */
export type ServerMoney = { minor: number; currency: string; decimals: number };
export function serverMoney(minor: number, currency = 'UGX', decimals = 2): ServerMoney {
  return { minor, currency, decimals };
}

/** `AssumptionLine` — schemas/common.py:31-45. */
export type ServerAssumptionLine = { key: string; value: number | string; source: string; note: string | null };
export const SERVER_ASSUMPTIONS: ServerAssumptionLine[] = [
  {
    key: 'recovery.rate',
    value: 0.35,
    source: 'config/economics.yaml',
    note: 'the band is r in {0.20, 0.35, 0.50}',
  },
  { key: 'four_eyes.threshold_exposure_minor', value: 50_000_000, source: 'config/economics.yaml', note: null },
];

/** `Meta` — schemas/common.py:72-90, all nine fields, `extra="forbid"`. */
export type ServerMeta = {
  run_id: string | null;
  trace_id: string | null;
  model_version: string | null;
  provenance: string | null;
  generated_at: string | null;
  assumptions: ServerAssumptionLine[];
  degraded: boolean;
  degraded_reason: string | null;
  disclaimer: string;
};
export function serverMeta(overrides: Partial<ServerMeta> = {}): ServerMeta {
  return {
    run_id: SERVER_RUN_ID,
    trace_id: '01J4Z7TRACE00000000000000',
    model_version: 'lgbm-fusion-4.5.3+iso',
    provenance: 'pipeline',
    generated_at: '2026-09-25T06:12:00Z',
    assumptions: SERVER_ASSUMPTIONS,
    degraded: false,
    degraded_reason: null,
    disclaimer:
      'OXBOW is a research prototype that analyzes historical, de-identified data only. It does not process live ' +
      'financial transactions, does not trade or advise on any financial instrument, does not make real financial ' +
      'decisions, and is not financial advice. Monetary figures are model estimates derived from stated assumptions, ' +
      'not measured outcomes. Results are not validated for operational use by any financial institution.',
    ...overrides,
  };
}

/** `Envelope` — schemas/common.py:106-112: `data` and `meta`, `extra="forbid"`, and no
 *  `success` key anywhere in this API. */
export function serverEnvelope<T>(data: T, meta: Partial<ServerMeta> = {}): { data: T; meta: ServerMeta } {
  return { data, meta: serverMeta(meta) };
}

/* --------------------------------------------------------- the decision write */

/** `DecisionCreate` — schemas/case.py:237-265. Four fields, `extra="forbid"`, and
 *  `reason` cannot be blank (`_not_blank` at :252-260). There is no `idempotency_key`:
 *  the server derives the outbox key as `sha256(run_id|case_id|decision_seq)`
 *  (routers/decisions.py:234-236), and `reversal_of_decision_id` defaults to None. */
export type ServerDecisionCreate = {
  action: ServerDecisionAction;
  reason: string;
  expected_version: number;
  reversal_of_decision_id: string | null;
};
export function decisionCreate(overrides: Partial<ServerDecisionCreate> = {}): ServerDecisionCreate {
  return {
    action: 'escalate',
    reason: 'Third cycle member identified; escalating with the loop attached.',
    expected_version: 3,
    reversal_of_decision_id: null,
    ...overrides,
  };
}

/** `DecisionWriteResult` — schemas/case.py:268-289, all twelve fields. `audit_seq` is
 *  the only optional one (`int | None = None` at :288). */
export type ServerDecisionWriteResult = {
  case_id: string;
  decision_id: string;
  decision_seq: number;
  chain_seq: number;
  row_hash: string;
  four_eyes_required: boolean;
  four_eyes_state: (typeof FOUR_EYES_STATES)[number];
  outbox_queued: boolean;
  case_version: number;
  decided_on_superseded_run: boolean;
  audit_seq: number | null;
  occurred_at: string;
};
export function decisionWriteResult(overrides: Partial<ServerDecisionWriteResult> = {}): ServerDecisionWriteResult {
  return {
    case_id: SERVER_CASE_ID,
    decision_id: SERVER_DECISION_ID,
    decision_seq: 3,
    chain_seq: 3,
    row_hash: '31b7e0c4d9a2f6510c8b3e5d7f9a1c2e4b6d8f0a2c4e6f80b1d3f5a7c9e0f213',
    four_eyes_required: true,
    // Above `four_eyes.threshold_exposure_minor` the row is chained and NOTHING is
    // queued: routers/decisions.py:10-15 says the client must be able to say that
    // without inferring a state from a missing field.
    four_eyes_state: 'pending',
    outbox_queued: false,
    case_version: 4,
    decided_on_superseded_run: false,
    audit_seq: 11,
    occurred_at: '2026-09-25T09:12:03Z',
    ...overrides,
  };
}

/** `FourEyesConfirm` — schemas/case.py:292-296. Two fields, both required. */
export type ServerFourEyesConfirm = { expected_version: number; confirmation_note: string };
export function fourEyesConfirm(overrides: Partial<ServerFourEyesConfirm> = {}): ServerFourEyesConfirm {
  return { expected_version: 4, confirmation_note: 'Read the loop; second reviewer concurs.', ...overrides };
}

/** The `current` mapping a 409 carries — built at apps/api/decisions.py:803-813 from the
 *  stored row, with `status` off the case snapshot rather than the decision. */
export type ServerStoredDecision = {
  decision_id: string;
  decision_seq: number;
  action: string;
  reason: string;
  actor_id: string;
  occurred_at: string;
  row_hash: string;
  four_eyes_state: string;
  status: string;
};
export function storedDecisionCurrent(overrides: Partial<ServerStoredDecision> = {}): ServerStoredDecision {
  return {
    decision_id: '01J4Z7M2QK9N7V1C4X6E8G0B2ED2',
    decision_seq: 3,
    action: 'dismiss',
    reason: 'Loop is an agent network of the same rail; closing with the note attached.',
    actor_id: 'reviewer.nabirye',
    occurred_at: '2026-09-25T09:20:00Z',
    row_hash: '77e0d9a2f6510c8b3e5d7f9a1c2e4b6d8f0a2c4e6f80b1d3f5a7c9e0f213ab12',
    four_eyes_state: 'not_required',
    status: 'dismissed',
    ...overrides,
  };
}

/** `ProblemDetail` (apps/api/problems.py:66-105) plus the three version-conflict
 *  members (`:100-113`, populated by `VersionConflict.to_problem` at :196-208). The
 *  `type` URI is `https://oxbow.dev/problems/` + the error *code*, and the code for a
 *  version conflict is `version-conflict` (:188). */
export const VERSION_CONFLICT_TYPE = 'https://oxbow.dev/problems/version-conflict';
export type ServerVersionConflictDocument = {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string | null;
  run_id: string | null;
  trace_id: string | null;
  retryable: boolean;
  expected_version: number;
  current_version: number;
  current: ServerStoredDecision;
};
export function versionConflict(
  current: ServerStoredDecision = storedDecisionCurrent(),
): ServerVersionConflictDocument {
  return {
    type: VERSION_CONFLICT_TYPE,
    title: 'Case changed since you loaded it',
    status: 409,
    detail: `case ${SERVER_CASE_ID} is at version 4; this write expected 3. Nobody's decision was overwritten.`,
    instance: SERVER_ROUTES.createDecision(SERVER_CASE_ID),
    run_id: SERVER_RUN_ID,
    trace_id: '01J4Z7TRACE00000000000000',
    retryable: false,
    expected_version: 3,
    current_version: 4,
    current,
  };
}

/* ------------------------------------------------------- the unreconciled read */

/** `CaseDetail` — schemas/case.py:197-234, its twenty-two top-level field names.
 *
 *  Listed rather than spelled out because the client's workspace decoder does NOT read
 *  this shape yet: `contract.ts` decodes a `CasePayload` (header/evidence/transactions/
 *  contributions, nested differently) that `apps/api` has never served. That is the
 *  biggest remaining §7 gap on the case route, and it is deliberately NOT papered over
 *  here — the names below are what a reconciliation pass has to hit. */
export const CASE_DETAIL_FIELDS = [
  'case_id',
  'pinned_run_id',
  'run_state',
  'superseded',
  'account_key',
  'band',
  'fused_score',
  'scorecard_points_total',
  'scorecard_points',
  'calibration',
  'predicted_typology',
  'model_version',
  'reason_codes',
  'rule_ids',
  'economics',
  'evidence',
  'transactions',
  'transaction_total',
  'shap',
  'rule_hits',
  'watchlist',
  'decision_history',
  'case_version',
  'status',
  'rank_under_active_policy',
  'counterfactual',
] as const;

/** `DecisionRecordRow` — schemas/case.py:173-194, the shape `decision_history` and
 *  `GET /api/cases/{case_id}/decisions` actually return. Note `action`, `actor_id`,
 *  `actor_roles`, `occurred_at`, `row_hash`, `decision_seq` — none of which is the
 *  `decision` / `actor` / `role` / `recorded_at` / `hash` / `seq` the client reads. */
export const DECISION_RECORD_ROW_FIELDS = [
  'decision_id',
  'decision_seq',
  'chain_seq',
  'action',
  'reason',
  'actor_id',
  'actor_roles',
  'occurred_at',
  'exposure',
  'four_eyes_required',
  'four_eyes_state',
  'confirmed_by',
  'confirmed_at',
  'reversal_of_decision_id',
  'decided_on_superseded_run',
  'prev_hash',
  'row_hash',
] as const;

/** `NetworkNode` — schemas/catalog.py:238-249. Its account identifier is `id`, whose
 *  description says it *is* an account key (:241). `flags` is a `list[str]`, written by
 *  `_node_flags` (routers/graph.py:336-352): the flags of the edges touching the account,
 *  plus `dense_community` from the stored community density and `flagged` from a D/E band. */
export const NETWORK_NODE_FIELDS = [
  'id',
  'label',
  'band',
  'exposure',
  'degree',
  'community_id',
  'is_seed',
  'is_rail',
  'flags',
] as const;

/** `NetworkEdge` — schemas/catalog.py:252-261. No edge id, no typology, no reversal flag,
 *  no velocity flag: the response model has never declared those names. */
export const NETWORK_EDGE_FIELDS = ['source', 'target', 'total', 'txn_count', 'first_ts', 'last_ts', 'flags'] as const;

/** `CommunityMetaNode` — schemas/catalog.py:264-283. */
export const COMMUNITY_META_NODE_FIELDS = [
  'community_id',
  'member_count',
  'total',
  'representative_account_key',
] as const;

/** `NetworkSubgraph` — schemas/catalog.py:283-302, twelve fields, `extra="forbid"`.
 *  `window_start` / `window_end` / `truncation_reason` / `counterparty_note` are nullable. */
export const NETWORK_SUBGRAPH_FIELDS = [
  'run_id',
  'seed_account_key',
  'hops',
  'nodes',
  'edges',
  'collapsed_communities',
  'node_cap',
  'truncated',
  'truncation_reason',
  'window_start',
  'window_end',
  'counterparty_note',
] as const;

export type ServerNetworkNode = {
  id: string;
  label: string;
  band: string | null;
  exposure: ServerMoney | null;
  degree: number;
  community_id: number | null;
  is_seed: boolean;
  is_rail: boolean;
  flags: string[];
};
export function serverNetworkNode(overrides: Partial<ServerNetworkNode> = {}): ServerNetworkNode {
  return {
    // `label` is the account key itself (routers/graph.py:228, `label=key`).
    id: 'ACC-7F2A19',
    label: 'ACC-7F2A19',
    band: 'E',
    exposure: serverMoney(180_000_000),
    degree: 6,
    community_id: 3,
    is_seed: false,
    is_rail: false,
    flags: ['flagged'],
    ...overrides,
  };
}

export type ServerNetworkEdge = {
  source: string;
  target: string;
  total: ServerMoney;
  txn_count: number;
  first_ts: string;
  last_ts: string;
  flags: string[];
};
export function serverNetworkEdge(overrides: Partial<ServerNetworkEdge> = {}): ServerNetworkEdge {
  return {
    source: 'ACC-7F2A19',
    target: 'ACC-00DORM',
    total: serverMoney(42_000_000),
    txn_count: 4,
    first_ts: '2026-09-03T09:14:00Z',
    last_ts: '2026-09-03T11:02:00Z',
    // The only value the pipeline writes here today: `["self_pair"]` for a self-transfer
    // and `[]` otherwise, at packages/pipeline/oxbow/adapters/warehouse/landing.py's
    // edges frame. `cycle` is in the `NetworkFlag` literal and is not written.
    flags: [],
    ...overrides,
  };
}

export type ServerCommunityMetaNode = {
  community_id: number;
  member_count: number;
  total: ServerMoney | null;
  representative_account_key: string;
};
export function serverCommunityMetaNode(overrides: Partial<ServerCommunityMetaNode> = {}): ServerCommunityMetaNode {
  return {
    community_id: 11,
    // `member_count` is the community's stored size, never the count this traversal
    // happened to reach (_meta_node, routers/graph.py:405-432).
    member_count: 812,
    total: serverMoney(9_400_000_000),
    representative_account_key: 'ACC-C0LLAP5ED',
    ...overrides,
  };
}

export type ServerNetworkSubgraph = {
  run_id: string;
  seed_account_key: string;
  hops: number;
  nodes: ServerNetworkNode[];
  edges: ServerNetworkEdge[];
  collapsed_communities: ServerCommunityMetaNode[];
  node_cap: number;
  truncated: boolean;
  truncation_reason: string | null;
  window_start: string | null;
  window_end: string | null;
  counterparty_note: string | null;
};

/** A whole `NetworkSubgraph`, the way `routers/graph.py:275-302` builds one: a seed, two
 *  one-hop accounts with the edges that reach them, one collapsed community whose
 *  representative is drawn as a meta-node, a stored window, and the node cap the run was
 *  configured with. `window_start`/`window_end` are non-null here because a bounded query
 *  is the common case; the tests that need the unbounded variant override them with null
 *  rather than this file inventing a date. */
export function networkSubgraph(overrides: Partial<ServerNetworkSubgraph> = {}): ServerNetworkSubgraph {
  const seed = serverNetworkNode({ id: 'ACC-7F2A19', label: 'ACC-7F2A19', is_seed: true, community_id: 3 });
  const oneHop = serverNetworkNode({
    id: 'ACC-00DORM',
    label: 'ACC-00DORM',
    band: 'D',
    degree: 2,
    community_id: 3,
    flags: ['flagged', 'dense_community'],
  });
  const rail = serverNetworkNode({
    id: 'ACC-R41L0G',
    label: 'ACC-R41L0G',
    band: null,
    exposure: null,
    degree: 41,
    community_id: null,
    is_rail: true,
    flags: [],
  });
  const representative = serverNetworkNode({
    id: 'ACC-C0LLAP5ED',
    label: 'ACC-C0LLAP5ED',
    band: null,
    exposure: null,
    degree: 812,
    community_id: 11,
    flags: ['dense_community'],
  });
  return {
    run_id: SERVER_RUN_ID,
    seed_account_key: 'ACC-7F2A19',
    hops: 2,
    nodes: [seed, oneHop, rail, representative],
    edges: [
      serverNetworkEdge({ source: 'ACC-7F2A19', target: 'ACC-00DORM', txn_count: 4 }),
      serverNetworkEdge({
        source: 'ACC-00DORM',
        target: 'ACC-R41L0G',
        txn_count: 11,
        first_ts: '2026-09-04T06:30:00Z',
        last_ts: '2026-09-04T06:31:00Z',
      }),
      serverNetworkEdge({
        source: 'ACC-7F2A19',
        target: 'ACC-C0LLAP5ED',
        txn_count: 1,
        first_ts: '2026-09-05T12:00:00Z',
        last_ts: '2026-09-05T12:00:00Z',
      }),
      // The one edge flag that is ever written: a self-transfer on the seed itself.
      serverNetworkEdge({
        source: 'ACC-R41L0G',
        target: 'ACC-R41L0G',
        txn_count: 2,
        first_ts: '2026-09-03T09:14:00Z',
        last_ts: '2026-09-03T09:15:00Z',
        flags: ['self_pair'],
      }),
    ],
    collapsed_communities: [serverCommunityMetaNode({ representative_account_key: 'ACC-C0LLAP5ED' })],
    node_cap: 1_500,
    truncated: false,
    truncation_reason: null,
    window_start: '2026-09-01T00:00:00Z',
    window_end: '2026-09-29T00:00:00Z',
    counterparty_note: null,
    ...overrides,
  };
}

/* ------------------------------------------------------------- validation route */

/** `ValidationBundle` — schemas/validation.py:231-255, fourteen top-level fields. The
 *  client's `Validation` decoder asks for a different fourteen (`baseline_table`,
 *  `pr_curve`, `operating_point`, `reliability`, `brier`, `calibration_floor`,
 *  `seeds`, `risk_adjusted`, `drawdown`, `shap_importance`, …), so `/api/validation`
 *  answers 200 with a body the decoder refuses. That refusal, and not a stall, is the
 *  state `/model` has to render — see tests/unit/stuck-panes-say-why.test.tsx. */
export const VALIDATION_BUNDLE_FIELDS = [
  'run_id',
  'corpora',
  'folds',
  'ablation',
  'curves',
  'confusion',
  'fairness',
  'perturbations',
  'metrics',
  'typology_recall',
  'overfitting',
  'label_quality',
  'limitations',
  'assumptions',
] as const;

/** `FoldRow` — schemas/validation.py:43-89, all its fields. */
export function foldRow(index: number): Record<string, unknown> {
  return {
    fold_index: index,
    corpus: 'ibm-aml',
    train_start: '2018-01-01',
    train_end: '2019-06-30',
    embargo_days: 30,
    embargo_end: '2019-07-30',
    test_start: '2019-07-31',
    test_end: '2019-12-31',
    n_train: 184_320,
    n_test: 42_118,
    pr_auc: 0.412,
    auroc: 0.906,
    brier: 0.031,
    precision_at_budget: 0.288,
    recall_at_budget: 0.411,
    precision_undefined: false,
    precision_note: null,
    alerts: 1_204,
    captured_value: serverMoney(1_820_000_000),
    cost: serverMoney(31_200_000),
    net_benefit: serverMoney(602_000_000),
    // Required by `FoldRow` (apps/api/schemas/validation.py:108-109) as a non-null Money, so
    // the double has to carry them: the tail of what was NOT reviewed is the figure plan §6.5
    // says decides whether a policy that wins on the mean is actually the better policy, and a
    // fold that rendered without it would hide the one number that can argue against the pick.
    var95: serverMoney(1_021_974_734),
    es975: serverMoney(1_602_515_847),
    max_drawdown: serverMoney(0),
    zero_drawdown_note: 'zero because the policy never lost money in these folds',
    monte_carlo_runs: 2_000,
    monte_carlo_seed: 1337,
    entity_disjoint: true,
    test_fold_touched_at: '2026-09-24T18:02:11Z',
  };
}

export function validationBundle(): Record<string, unknown> {
  return {
    run_id: SERVER_RUN_ID,
    corpora: ['ibm-aml', 'paysim'],
    folds: [foldRow(0), foldRow(1)],
    ablation: [],
    curves: [],
    confusion: null,
    fairness: [],
    perturbations: [],
    metrics: [],
    typology_recall: [],
    overfitting: {},
    label_quality: {},
    limitations: ['Two public corpora, neither of which is East-African mobile money.'],
    assumptions: SERVER_ASSUMPTIONS,
  };
}
