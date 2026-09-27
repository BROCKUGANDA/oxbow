/* =============================================================================
   Case workspace — plan §14 P8a-3. Three panes, one decision.

   CROSS-FILTER. Selecting a contribution filters the evidence timeline and the
   transaction table to the ids that contribution names. This is the demo's hinge:
   "click the top SHAP contribution — the transaction table filters to exactly the
   three transactions that caused it". The id sets come from the response, so the
   filtered view is a claim about the data rather than a client-side guess about
   similarity.

   useOptimistic. A decision appears in the append-only log at once with a hash chip
   and an explicit "not yet written" marker, reconciles on the receipt, and rolls back
   to an inline error if the write fails. The updater is called inside `startTransition`
   — that wrapper is what makes the revert real: `useOptimistic` state only exists for
   as long as the transition that created it is open, so a 409 closes the transition,
   the pending row goes away, and the log shows only what the server actually wrote.
   A 409 keeps the analyst's text in the box and shows what was written instead — the
   merge view, read from the `current` row the API hands the loser, rather than silently
   overwriting a colleague.

   THE WRITE, EXACTLY AS THE API DECLARES IT. `POST /api/cases/{case_id}/decisions`
   (`apps/api/routers/decisions.py:41-57`), keyed on the 26-character ULID `case_id`,
   with the `DecisionCreate` body (`schemas/case.py:237-265`) and a `DecisionWriteResult`
   receipt (`:268-289`). The route used to post to `/api/cases/{account_key}` with a
   `decision` field and a client-chosen `idempotency_key`: the path parameter is
   `Path(min_length=26, max_length=26)`, so it never reached a handler, and the model is
   `extra="forbid"`, so the extra key alone was a 422. A four-eyes decision above the
   threshold is now followed by its second reviewer's confirm
   (`POST /api/decisions/{decision_id}/confirm`, `routers/decisions.py:91-128`), which is
   the only thing that queues its outbox row.

   SUSPENSE ORDER. The score header is the persuasive content, so it resolves first and
   the SHAP and evidence panes after it. Each pane reserves its own geometry, so a late
   arrival pushes nothing: that is the zero-CLS claim on this route.
   ============================================================================= */

'use client';

import { useParams, useSearchParams } from 'next/navigation';
import { type CSSProperties, type ReactElement, Suspense, useState, useTransition } from 'react';
import { useOptimistic } from 'react';

import { Pane } from '@/components/Pane';
import { Waterfall, type WaterfallRow } from '@/components/charts/charts';
import { TYPOLOGY_META, glyphFor } from '@/components/typology';
import { BandBadge } from '@/components/ui/BandBadge';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { AccountChip, Timestamp } from '@/components/ui/provenance';
import { ELLIPSIS, HAIRLINE_BOTTOM, PANEL_SUNKEN, T_BODY, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';
import { Icon } from '@/design/icons/Icon';
import {
  type AssumptionLine,
  type CasePayload,
  type Decision,
  type DecisionAction,
  type DecisionCreateBody,
  type DecisionWriteResult,
  DecisionWriteResultDecoder,
  type FourEyesConfirmBody,
  type ListMeta,
  ROUTES,
  caseDecisionsPath,
  casePath,
  decisionConfirmPath,
} from '@/lib/api/contract';
import { useResource, useRuntime, useWrite } from '@/lib/api/hooks';
import { ApiError, type ApiFailure, failureDetail, failureFields, failureTitle } from '@/lib/api/problem';
import { compactFromMinor, count } from '@/lib/format/money';
import { formatDuration } from '@/lib/format/time';

/** What a decision looks like before the server has agreed to it. */
type DraftDecision = { decision: DecisionAction; reason: string };

/** The queue's `e`/`d` shortcuts land here, so the rail can preselect an action. */
const DEEP_ACTIONS: readonly DecisionAction[] = ['review', 'escalate', 'dismiss'];

/** `?decide=` is free text from a URL: an unrecognised word preselects nothing rather
 *  than being coerced into an action the analyst did not ask for. */
function deepActionOf(raw: string | null): DecisionAction | null {
  return DEEP_ACTIONS.find((action) => action === raw) ?? null;
}

const CONTROL: CSSProperties = {
  ...T_LABEL,
  padding: '6px 8px',
  border: '1px solid var(--color-hairline-strong)',
  borderRadius: 'var(--radius-control)',
  background: 'transparent',
  color: 'var(--color-ink)',
  cursor: 'pointer',
};

export default function CasePage(): ReactElement {
  return (
    <Suspense fallback={<CaseRouteSkeleton />}>
      <CaseWorkspace />
    </Suspense>
  );
}

/** The three-column workspace geometry, reserved before the query string is readable.
 *  Same columns and row counts as the resolved pane layout, so the boundary costs no
 *  shift — the same reason the in-pane skeletons are matched to their content. */
function CaseRouteSkeleton(): ReactElement {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 340px) minmax(0, 1fr) minmax(0, 340px)',
        gap: 'var(--spacing-pane-gap)',
        padding: 'var(--spacing-pane-gap)',
        alignItems: 'start',
      }}
    >
      <Pane
        id="score"
        title="Score"
        operation="Loading the score header"
        skeleton={{ columns: [{ key: 'score', width: '100%' }], rows: 5 }}
      >
        <span />
      </Pane>
      <Pane
        id="evidence"
        title="Evidence"
        operation="Loading the evidence pane"
        skeleton={{ columns: [{ key: 'row', width: '100%' }], rows: 9, rowHeight: 36 }}
      >
        <span />
      </Pane>
      <Pane
        id="decision"
        title="Decision"
        operation="Loading the decision rail"
        skeleton={{ columns: [{ key: 'rail', width: '100%' }], rows: 4 }}
      >
        <span />
      </Pane>
    </div>
  );
}

function CaseWorkspace(): ReactElement {
  const routeParams = useParams<{ id: string }>();
  const search = useSearchParams();
  /* The workspace is keyed on `case_id` — the 26-character ULID the API's path parameter
     demands (`Path(min_length=26, max_length=26)` at apps/api/routers/cases.py:80) — and
     not on the account key, which is a different identifier for a different route. */
  const caseId = decodeURIComponent(routeParams.id ?? '');
  const path = casePath(caseId);
  const decisionsPath = caseDecisionsPath(caseId);
  const caseResource = useResource(`case:${caseId}`, path, ROUTES.case.data);
  const runtime = useRuntime();

  const [filter, setFilter] = useState<{ feature: string; txnIds: readonly string[] } | null>(null);
  const [tab, setTab] = useState<'why' | 'timeline' | 'transactions' | 'rules' | 'counterfactual'>('why');
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [writeFailure, setWriteFailure] = useState<ApiFailure | null>(null);
  /** The last receipt the server actually issued. Everything the four-eyes panel says
   *  comes from here, so it never reports a state the API did not report. */
  const [receipt, setReceipt] = useState<DecisionWriteResult | null>(null);
  const [note, setNote] = useState('');
  const [confirming, startConfirming] = useTransition();

  const recorded = caseResource.data?.decisions ?? [];
  const [optimistic, applyOptimistic] = useOptimistic<Decision[], DraftDecision>(recorded, (current, draft) => [
    ...current,
    {
      /* A pending row carries only what is genuinely known: the action and the text the
         analyst typed, and the chain head read from the rows already on screen. No seq,
         no actor, no timestamp, no hash — the receipt supplies all four, and until it
         arrives the renderer says "not yet written" instead of a value nobody sent. */
      seq: 0,
      decision: draft.decision,
      reason: draft.reason,
      actor: '',
      role: '',
      recorded_at: '',
      hash: 'pending',
      prev_hash: current.at(-1)?.hash ?? null,
      reversible_of: null,
      four_eyes_required: false,
      superseded_run: false,
      pending: true,
    },
  ]);

  const write = useWrite<DecisionCreateBody, DecisionWriteResult>(decisionsPath, DecisionWriteResultDecoder, [
    ['case', path],
  ]);

  /* The second reviewer's write. `decision_id` only exists once the first one has been
     answered, so the path is built from the receipt rather than guessed at. */
  const confirm = useWrite<FourEyesConfirmBody, DecisionWriteResult>(
    receipt === null ? decisionsPath : decisionConfirmPath(receipt.decision_id),
    DecisionWriteResultDecoder,
    [['case', path]],
  );

  const [writing, startWriting] = useTransition();

  if (caseResource.data === null) {
    return <CaseSkeleton resource={caseResource} caseId={caseId} />;
  }

  const payload = caseResource.data;
  const assumptions = caseResource.meta?.assumptions ?? [];
  const timeZone = runtime.data?.deployment_timezone ?? null;

  const txnSet = filter === null ? null : new Set(filter.txnIds);
  const evidence =
    filter === null
      ? payload.evidence
      : payload.evidence.filter((event) => event.txn_ids.some((id) => txnSet?.has(id) ?? false));
  const transactions =
    filter === null ? payload.transactions : payload.transactions.filter((txn) => txnSet?.has(txn.txn_id) ?? false);

  const submit = async (decision: DecisionAction): Promise<void> => {
    const text = reason.trim();
    if (text.length === 0) {
      setReasonError('A written reason is required. A decision without one cannot be defended in a packet.');
      return;
    }
    setReasonError(null);
    setWriteFailure(null);
    setReceipt(null);
    /* `useOptimistic` keeps its extra row for exactly as long as the transition that
       created it is open. Called outside one, the updater is refused outright — React
       logs "An optimistic state update occurred outside a transition" and the pending
       row never appears — and a rejection then has nothing to roll back, which is how
       the log came to keep a `pending`/`you`/empty-timestamp row forever. */
    startWriting(async () => {
      applyOptimistic({ decision, reason: text });
      try {
        const written = await write.mutateAsync({
          action: decision,
          reason: text,
          expected_version: payload.decision_version,
          reversal_of_decision_id: null,
        });
        setReceipt(written.data);
        setReason('');
      } catch (error) {
        // The transition closing is what drops the optimistic row; the message renders
        // where the analyst typed, not in a toast they will have scrolled past.
        setWriteFailure(error instanceof ApiError ? error.failure : null);
      }
    });
  };

  const confirmFourEyes = async (): Promise<void> => {
    const text = note.trim();
    if (text.length === 0) {
      setReasonError('The second reviewer signs the confirmation with their own sentence.');
      return;
    }
    setReasonError(null);
    setWriteFailure(null);
    startConfirming(async () => {
      try {
        const confirmed = await confirm.mutateAsync({
          expected_version: receipt?.case_version ?? 0,
          confirmation_note: text,
        });
        setReceipt(confirmed.data);
        setNote('');
      } catch (error) {
        setWriteFailure(error instanceof ApiError ? error.failure : null);
      }
    });
  };

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 340px) minmax(0, 1fr) minmax(0, 340px)',
        gap: 'var(--spacing-pane-gap)',
        padding: 'var(--spacing-pane-gap)',
        alignItems: 'start',
      }}
    >
      {/* ----------------------------------------------------- left pane -- */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)' }}>
        <Pane
          id="score"
          title="Score"
          operation="Loading the score header"
          meta={caseResource.meta}
          skeleton={{ columns: [{ key: 'score', width: '100%' }], rows: 5 }}
        >
          <ScoreHeader payload={payload} />
        </Pane>

        <Pane
          id="points"
          title="Scorecard points"
          operation="Loading the points table"
          meta={caseResource.meta}
          skeleton={{
            columns: [
              { key: 'attr', width: '62%' },
              { key: 'pts', width: '38%', align: 'end' },
            ],
            rows: Math.max(payload.header.points.length, 3),
          }}
        >
          <PointsTable payload={payload} onSelect={setFilter} filter={filter} />
        </Pane>

        <Pane
          id="economics"
          title="Economics"
          operation="Loading the economics block"
          meta={caseResource.meta}
          skeleton={{ columns: [{ key: 'e', width: '100%' }], rows: 4 }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <MoneyFigure
              figure={payload.header.economics.exposure}
              assumptions={assumptions}
              label="Exposure at risk"
              source={caseResource.meta === null ? null : 'config/economics.yaml'}
            />
            <MoneyFigure
              figure={payload.header.economics.expected_value}
              assumptions={assumptions}
              label="Expected value of reviewing"
              source={caseResource.meta === null ? null : 'EV = p·E·r − c − (1−p)·f'}
            />
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
              review cost{' '}
              {compactFromMinor(
                payload.header.economics.analyst_cost.value.minor,
                payload.header.economics.analyst_cost.value.decimals,
              )}{' '}
              {payload.header.economics.analyst_cost.value.currency} for{' '}
              {formatDuration(payload.header.economics.review_minutes)} · friction if wrongly touched{' '}
              {compactFromMinor(
                payload.header.economics.friction_cost.minor,
                payload.header.economics.friction_cost.decimals,
              )}
            </p>
            <MonteCarlo payload={payload} />
          </div>
        </Pane>
      </div>

      {/* --------------------------------------------------- centre pane -- */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)', minWidth: 0 }}>
        <Pane
          id="evidence"
          title={filter === null ? 'Evidence' : `Evidence · filtered by ${filter.feature}`}
          operation="Loading the evidence pane"
          meta={caseResource.meta}
          actions={
            filter !== null ? (
              <button type="button" onClick={() => setFilter(null)} style={CONTROL}>
                clear cross-filter
              </button>
            ) : null
          }
          skeleton={{ columns: [{ key: 'row', width: '100%' }], rows: 9, rowHeight: 36 }}
        >
          <Tabs tab={tab} onChange={setTab} />
          {tab === 'why' ? <WhyPanel payload={payload} filter={filter} onFilter={setFilter} /> : null}
          {tab === 'timeline' ? (
            <Timeline payload={payload} evidence={evidence} timeZone={timeZone} filter={filter} />
          ) : null}
          {tab === 'transactions' ? (
            <Transactions payload={payload} transactions={transactions} timeZone={timeZone} />
          ) : null}
          {tab === 'rules' ? <RuleHits payload={payload} timeZone={timeZone} /> : null}
          {tab === 'counterfactual' ? <CounterfactualPanel payload={payload} assumptions={assumptions} /> : null}
        </Pane>

        <Pane
          id="narrative"
          title="Case narrative"
          operation="Loading the narrative"
          meta={caseResource.meta}
          skeleton={{ columns: [{ key: 'n', width: '100%' }], rows: 2 }}
        >
          <Narrative payload={payload} />
        </Pane>
      </div>

      {/* ---------------------------------------------------- right pane -- */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)' }}>
        <DecisionRail
          payload={payload}
          assumptions={assumptions}
          meta={caseResource.meta}
          onDecide={submit}
          failure={writeFailure}
          pending={writing || write.pending || confirming || confirm.pending}
          reason={reason}
          setReason={setReason}
          reasonError={reasonError}
          receipt={receipt}
          note={note}
          setNote={setNote}
          onConfirm={confirmFourEyes}
          confirmFailure={confirm.failure}
          deepAction={deepActionOf(search.get('decide'))}
        />

        <Pane
          id="history"
          title="Decision history"
          operation="Loading the decision log"
          meta={caseResource.meta}
          skeleton={{ columns: [{ key: 'hash', width: '100%' }], rows: Math.max(optimistic.length, 2) }}
        >
          <DecisionHistory decisions={optimistic} timeZone={timeZone} />
        </Pane>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- skeleton --- */

function CaseSkeleton({
  resource,
  caseId,
}: { resource: ReturnType<typeof useResource<CasePayload>>; caseId: string }): ReactElement {
  const loading = resource.isPending && resource.failure === null;
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 340px) minmax(0, 1fr) minmax(0, 340px)',
        gap: 'var(--spacing-pane-gap)',
        padding: 'var(--spacing-pane-gap)',
        alignItems: 'start',
      }}
    >
      <Pane
        id="score"
        title="Score"
        operation="Loading the score header"
        failure={resource.failure}
        pending={resource.isPending}
        onRetry={() => void resource.refetch()}
        attempt={resource.attempts}
        retrying={resource.isFetching}
        skeleton={{ columns: [{ key: 'score', width: '100%' }], rows: 5 }}
      >
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
          {loading ? `Reading ${caseId} from the recorded run.` : 'Nothing returned.'}
        </p>
      </Pane>
      <Pane
        id="evidence"
        title="Evidence"
        operation="Loading the evidence pane"
        failure={resource.failure}
        pending={resource.isPending}
        onRetry={() => void resource.refetch()}
        attempt={resource.attempts}
        retrying={resource.isFetching}
        skeleton={{ columns: [{ key: 'row', width: '100%' }], rows: 9, rowHeight: 36 }}
      >
        <span />
      </Pane>
      <Pane
        id="decision"
        title="Decision"
        operation="Loading the decision rail"
        failure={resource.failure}
        pending={resource.isPending}
        onRetry={() => void resource.refetch()}
        attempt={resource.attempts}
        retrying={resource.isFetching}
        skeleton={{ columns: [{ key: 'rail', width: '100%' }], rows: 4 }}
      >
        <span />
      </Pane>
    </div>
  );
}

/* --------------------------------------------------------------- pieces --- */

function ScoreHeader({ payload }: { payload: CasePayload }): ReactElement {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <AccountChip
          accountKey={payload.header.account_key}
          href={`/network?account=${encodeURIComponent(payload.header.account_key)}`}
        />
        <BandBadge band={payload.header.band} />
      </div>
      <p
        className="u-num"
        style={{ ...T_MONO, fontSize: 'var(--text-hero)', fontWeight: 600, margin: 0, color: 'var(--color-ink)' }}
      >
        {payload.header.score.toFixed(3)}
      </p>
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '40ch' }}>
        calibrated probability · observed rate in this band{' '}
        <strong className="u-num" style={{ color: 'var(--color-ink)' }}>
          {(payload.header.calibration.observed_rate * 100).toFixed(0)}%
        </strong>{' '}
        · n={count(payload.header.calibration.n)}
      </p>
      {payload.header.typology !== null ? (
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
          <Icon
            name={glyphFor(payload.header.typology)}
            size={13}
            title={TYPOLOGY_META[payload.header.typology].name}
          />{' '}
          {TYPOLOGY_META[payload.header.typology].name} — {TYPOLOGY_META[payload.header.typology].reads}
        </p>
      ) : null}
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        run <code style={T_MONO}>{payload.header.run_id}</code> · model {payload.header.model_version ?? 'unrecorded'} ·
        feature spec {payload.header.feature_spec_hash ?? 'unrecorded'} ·{' '}
        {payload.header.decided_on_superseded_run
          ? 'decided on a superseded run, stamped as such in the audit row'
          : 'scored on the current run'}
      </p>
      {payload.header.fusion !== null ? (
        <p
          style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}
          title="the fusion meta-learner prints its own coefficients"
        >
          fused from scorecard {payload.header.fusion.p_scorecard.toFixed(3)} and GBM{' '}
          {payload.header.fusion.p_gbm.toFixed(3)}; weights{' '}
          {payload.header.fusion.coefficients.map((entry) => `${entry.input} ${entry.weight.toFixed(2)}`).join(', ')}
        </p>
      ) : null}
    </div>
  );
}

function PointsTable({
  payload,
  filter,
  onSelect,
}: {
  payload: CasePayload;
  filter: { feature: string; txnIds: readonly string[] } | null;
  onSelect: (next: { feature: string; txnIds: readonly string[] } | null) => void;
}): ReactElement {
  const byFeature = new Map(payload.contributions.map((entry) => [entry.feature, entry]));
  return (
    <>
      <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {payload.header.points.map((point) => {
          const contribution = byFeature.get(point.attribute);
          const active = filter?.feature === point.attribute;
          const row = (
            <>
              <span>
                <span
                  style={{ ...T_LABEL, color: 'var(--color-ink)', display: 'block', ...ELLIPSIS }}
                  title={point.label}
                >
                  {point.label}
                </span>
                <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                  bin {point.bin} · {(point.population_share * 100).toFixed(1)}% of population · bad rate{' '}
                  {(point.bad_rate * 100).toFixed(1)}%
                </span>
              </span>
              <span
                className="u-num"
                style={{ ...T_MONO, color: point.points < 0 ? 'var(--color-band-e)' : 'var(--color-band-b)' }}
              >
                {point.points > 0 ? `+${String(point.points)}` : String(point.points)}
              </span>
            </>
          );
          return (
            <li
              key={point.attribute}
              style={{
                display: 'grid',
                gridTemplateColumns: 'minmax(0,1fr) auto',
                gap: 8,
                alignItems: 'baseline',
                padding: '4px 0',
                ...HAIRLINE_BOTTOM,
              }}
            >
              {contribution !== undefined && contribution.txn_ids.length > 0 ? (
                <button
                  type="button"
                  onClick={() =>
                    onSelect(active ? null : { feature: contribution.feature, txnIds: contribution.txn_ids })
                  }
                  aria-pressed={active}
                  title="Cross-filter the evidence and transaction panes to the transactions behind this attribute"
                  style={{
                    display: 'contents',
                    background: active ? 'var(--color-elev-2)' : 'transparent',
                    border: 'none',
                    color: 'inherit',
                    font: 'inherit',
                    textAlign: 'left',
                    cursor: 'pointer',
                  }}
                >
                  {row}
                </button>
              ) : (
                row
              )}
            </li>
          );
        })}
      </ol>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
        Whole-number points per bin, summing to the score above: the column adds up by hand, which is the point of a
        scorecard.
      </p>
    </>
  );
}

function MonteCarlo({ payload }: { payload: CasePayload }): ReactElement {
  const interval = payload.header.economics.monte_carlo;
  if (interval === null) {
    return (
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        No Monte Carlo interval is recorded for this case. The point estimate above is the only exposure figure this run
        produced, and it is shown as that rather than widened by guesswork.
      </p>
    );
  }
  return (
    <div data-monte-carlo style={{ ...PANEL_SUNKEN, padding: 10 }}>
      <p style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em', margin: 0 }}>
        Exposure if unactioned
      </p>
      <p
        className="u-num"
        style={{
          ...T_MONO,
          fontSize: 'var(--text-kpi)',
          fontWeight: 600,
          margin: '4px 0 0',
          color: 'var(--color-ink)',
        }}
      >
        {compactFromMinor(interval.lower.minor, interval.lower.decimals)} –{' '}
        {compactFromMinor(interval.upper.minor, interval.upper.decimals)} {interval.upper.currency}
      </p>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 4 }}>
        90% interval · {count(interval.runs)} seeded propagation runs at depth {String(interval.max_depth)}, seed{' '}
        {String(interval.seed)}. Each outgoing edge transmits with probability proportional to its value share.
      </p>
    </div>
  );
}

const TABS: readonly { id: 'why' | 'timeline' | 'transactions' | 'rules' | 'counterfactual'; label: string }[] = [
  { id: 'why', label: 'Why this was flagged' },
  { id: 'timeline', label: 'Evidence timeline' },
  { id: 'transactions', label: 'Transactions' },
  { id: 'rules', label: 'Rule hits' },
  { id: 'counterfactual', label: 'Counterfactual' },
];

function Tabs({
  tab,
  onChange,
}: {
  tab: 'why' | 'timeline' | 'transactions' | 'rules' | 'counterfactual';
  onChange: (next: typeof tab) => void;
}): ReactElement {
  return (
    <div role="tablist" style={{ display: 'flex', gap: 2, marginBottom: 10, flexWrap: 'wrap' }}>
      {TABS.map((entry) => (
        <button
          key={entry.id}
          type="button"
          role="tab"
          aria-selected={tab === entry.id}
          onClick={() => onChange(entry.id)}
          style={{
            ...CONTROL,
            color: tab === entry.id ? 'var(--color-ink)' : 'var(--color-ink-muted)',
            background: tab === entry.id ? 'var(--color-elev-2)' : 'transparent',
            border: `1px solid ${tab === entry.id ? 'var(--color-evidence)' : 'var(--color-hairline)'}`,
          }}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}

function WhyPanel({
  payload,
  filter,
  onFilter,
}: {
  payload: CasePayload;
  filter: { feature: string; txnIds: readonly string[] } | null;
  onFilter: (next: { feature: string; txnIds: readonly string[] } | null) => void;
}): ReactElement {
  const rows: WaterfallRow[] = payload.contributions.map((contribution) => ({
    label: contribution.label,
    value: contribution.value,
    direction: contribution.direction,
    selected: filter?.feature === contribution.feature,
    onSelect: () =>
      onFilter(
        filter?.feature === contribution.feature
          ? null
          : { feature: contribution.feature, txnIds: contribution.txn_ids },
      ),
  }));
  const named =
    filter === null
      ? 0
      : (payload.contributions.find((entry) => entry.feature === filter.feature)?.txn_ids.length ?? 0);
  return (
    <div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '72ch' }}>
        {payload.contributions.length > 0 && payload.contributions[0]?.source === 'shap'
          ? 'TreeExplainer values persisted with the scored row — this panel computes nothing on request. Selecting a contribution filters the timeline and the transaction table to the transactions that caused it.'
          : 'SHAP is unavailable because this row fell through a degenerate tree, so the waterfall below is the scorecard’s own point contributions, labelled as such.'}
      </p>
      <Waterfall
        rows={rows}
        ariaLabel="Contribution waterfall for this case"
        formatValue={(value) => value.toFixed(3)}
      />
      {filter !== null ? (
        <p style={{ ...T_MICRO, color: 'var(--color-evidence)', marginTop: 8 }} data-cross-filter>
          filtering the other panes to the {count(named)} transactions named by {filter.feature}
        </p>
      ) : null}
    </div>
  );
}

function Timeline({
  payload,
  evidence,
  timeZone,
  filter,
}: {
  payload: CasePayload;
  evidence: CasePayload['evidence'];
  timeZone: string | null;
  filter: { feature: string; txnIds: readonly string[] } | null;
}): ReactElement {
  if (evidence.length === 0) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '72ch' }}>
        {filter === null
          ? `No events were recorded for ${payload.header.account_key} inside the window that produced this score. A quiet window is a property of the window, so widen it before reading this as a clean account.`
          : `No evidence event names a transaction from ${filter.feature}, although ${count(payload.evidence.length)} events exist. The contribution is carried by the feature vector rather than by a single transfer.`}
      </p>
    );
  }
  return (
    <ol className="u-scroll" style={{ listStyle: 'none', margin: 0, padding: 0, maxHeight: 320, overflowY: 'auto' }}>
      {evidence.map((event) => (
        <li
          key={event.id}
          style={{
            display: 'grid',
            gridTemplateColumns: '170px minmax(0,1fr) 130px',
            gap: 8,
            padding: '6px 0',
            ...HAIRLINE_BOTTOM,
          }}
        >
          <Timestamp iso={event.ts_utc} timeZone={timeZone} />
          <span style={{ minWidth: 0 }}>
            <span style={{ ...T_LABEL, color: 'var(--color-ink)', display: 'block', ...ELLIPSIS }} title={event.title}>
              {event.typology !== null ? <Icon name={glyphFor(event.typology)} size={13} /> : null} {event.title}
            </span>
            <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>{event.detail}</span>
          </span>
          <span className="u-num" style={{ ...T_LABEL, textAlign: 'right', color: 'var(--color-ink-muted)' }}>
            {event.amount === null
              ? '—'
              : `${compactFromMinor(event.amount.minor, event.amount.decimals)} ${event.amount.currency}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

function Transactions({
  payload,
  transactions,
  timeZone,
}: { payload: CasePayload; transactions: CasePayload['transactions']; timeZone: string | null }): ReactElement {
  if (transactions.length === 0) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '72ch' }}>
        No transaction in this window matches the current cross-filter, although {count(payload.transactions.length)}{' '}
        were returned for the case.
      </p>
    );
  }
  return (
    <div className="u-scroll" style={{ maxHeight: 360, overflow: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--text-label)' }}>
        <thead>
          <tr>
            {['transaction', 'when', 'type', 'counterparty', 'amount', 'rules'].map((heading) => (
              <th
                key={heading}
                style={{
                  textAlign: heading === 'amount' ? 'right' : 'left',
                  ...HAIRLINE_BOTTOM,
                  padding: '4px 6px',
                  color: 'var(--color-ink-faint)',
                  fontWeight: 500,
                }}
              >
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {transactions.map((txn) => (
            <tr key={txn.txn_id} style={{ height: 'var(--spacing-table-row)' }}>
              <td style={{ ...T_MONO, fontSize: 'var(--text-micro)', padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                {txn.txn_id}
              </td>
              <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                <Timestamp iso={txn.ts_utc} timeZone={timeZone} />
              </td>
              <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}>
                {txn.type} · {txn.direction}
              </td>
              <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, ...T_MONO, fontSize: 'var(--text-micro)' }}>
                {txn.counterparty_key ?? 'external'}
              </td>
              <td
                className="u-num"
                style={{
                  padding: '4px 6px',
                  textAlign: 'right',
                  ...HAIRLINE_BOTTOM,
                  color: txn.is_zero_value ? 'var(--color-ink-faint)' : 'var(--color-ink)',
                }}
              >
                {compactFromMinor(txn.amount.minor, txn.amount.decimals)} {txn.amount.currency}
                {txn.is_zero_value ? ' · zero value, kept and flagged' : ''}
              </td>
              <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}>
                {txn.rule_codes.join(', ') || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RuleHits({ payload, timeZone }: { payload: CasePayload; timeZone: string | null }): ReactElement {
  if (payload.rule_hits.length === 0) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '72ch' }}>
        No rule fired on this account. Its score comes from the tabular and graph channels alone, which is a different
        claim from “nothing was found”.
      </p>
    );
  }
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
      {payload.rule_hits.map((hit) => (
        <li key={hit.rule_code} data-rule-hit={hit.rule_code} style={{ ...PANEL_SUNKEN, padding: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <Icon name={glyphFor(hit.typology)} size={16} title={TYPOLOGY_META[hit.typology].name} />
            <strong style={{ ...T_LABEL, color: 'var(--color-ink)', fontWeight: 600 }}>
              {hit.rule_code} {hit.name}
            </strong>
            <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
              severity {(hit.severity * 100).toFixed(0)} of 100
            </span>
            {hit.overlap_group !== null ? (
              <span
                style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}
                title="overlapping rules are grouped; the score counts the group once"
              >
                overlap group {hit.overlap_group} · {hit.counted_once ? 'counted once' : 'counts again'}
              </span>
            ) : null}
          </div>
          <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: '4px 0' }}>{hit.observed}</p>
          <p
            style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}
            title="thresholds are config/rules.yaml values, fitted on the training window"
          >
            parameters {hit.parameters.map((entry) => `${entry.key}=${String(entry.value)}`).join(' · ')} · first{' '}
            <Timestamp iso={hit.first_hit} timeZone={timeZone} /> · last{' '}
            <Timestamp iso={hit.last_hit} timeZone={timeZone} />
          </p>
        </li>
      ))}
    </ul>
  );
}

function CounterfactualPanel({
  payload,
  assumptions,
}: { payload: CasePayload; assumptions: readonly AssumptionLine[] }): ReactElement {
  const counterfactual = payload.counterfactual;
  if (counterfactual === null) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '72ch' }}>
        No counterfactual is computed for this case: the server reports that no single contribution is large enough that
        removing it changes the band. That is a finding about how this score is built, not a missing panel.
      </p>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <p style={T_BODY}>{counterfactual.statement}</p>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <p style={{ ...T_LABEL, margin: 0 }}>
          score without{' '}
          <span className="u-num" style={{ ...T_MONO, color: 'var(--color-ink)' }}>
            {counterfactual.score_without.toFixed(3)}
          </span>
          {counterfactual.band_without !== null ? (
            <>
              {' · '}
              <BandBadge band={counterfactual.band_without} describe={false} />
            </>
          ) : null}
        </p>
        <MoneyFigure
          figure={counterfactual.expected_value_without}
          assumptions={assumptions}
          label="Expected value without it"
          compact
        />
      </div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        Computed by the server on the same feature matrix as the score. This client re-derives nothing.
      </p>
    </div>
  );
}

function Narrative({ payload }: { payload: CasePayload }): ReactElement {
  if (payload.narrative === null) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '72ch' }}>
        No narrative is recorded for this case, and nothing has been written in its place.
      </p>
    );
  }
  return (
    <div>
      <p style={T_BODY}>{payload.narrative.text}</p>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 6 }}>
        source: {payload.narrative.source === 'template' ? 'deterministic template' : 'summariser'} ·{' '}
        {payload.narrative.degraded
          ? 'the summariser port is unavailable on this deployment, so this is the labelled fallback rather than no text'
          : 'generated for this run'}
      </p>
    </div>
  );
}

function DecisionRail({
  payload,
  assumptions,
  meta,
  onDecide,
  failure,
  pending,
  reason,
  setReason,
  reasonError,
  receipt,
  note,
  setNote,
  onConfirm,
  confirmFailure,
  deepAction,
}: {
  payload: CasePayload;
  assumptions: readonly AssumptionLine[];
  /** The case response's own provenance block, threaded like every other pane on the
   *  route. It used to be `null` here *together with a skeleton spec*, and a pane with
   *  no meta and a skeleton renders the skeleton — so the rail never rendered its own
   *  children and the decision form was absent from the DOM on a fully-loaded case. */
  meta: ListMeta | null;
  onDecide: (decision: DecisionAction) => Promise<void>;
  failure: ApiFailure | null;
  pending: boolean;
  reason: string;
  setReason: (next: string) => void;
  reasonError: string | null;
  receipt: DecisionWriteResult | null;
  note: string;
  setNote: (next: string) => void;
  onConfirm: () => Promise<void>;
  confirmFailure: ApiFailure | null;
  deepAction: string | null;
}): ReactElement {
  const choices: readonly { id: DecisionAction; label: string; hint: string }[] = [
    { id: 'review', label: 'Review', hint: 'keep it on the desk' },
    { id: 'escalate', label: 'Escalate', hint: 'send it onward with the evidence attached' },
    { id: 'dismiss', label: 'Dismiss', hint: 'close it, and say why' },
  ];
  const fields = failure === null ? [] : failureFields(failure);
  const conflict =
    failure !== null && failure.kind === 'problem' && failure.class === 'conflict' ? failure.problem.conflict : null;
  const exposure = payload.header.economics.exposure.value;
  /* Four-eyes is a state the server reports, not one the client infers: `pending` means
     the row is chained but no delivery has been promised (`outbox_queued: false`) until a
     second, different reviewer confirms it — apps/api/routers/decisions.py:10-15. */
  const awaitingSecondReviewer = receipt !== null && receipt.four_eyes_state === 'pending';

  return (
    <Pane
      id="decision"
      title="Decision"
      operation="Recording the decision"
      meta={meta}
      skeleton={{ columns: [{ key: 'rail', width: '100%' }], rows: 4 }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <label style={{ ...T_LABEL, display: 'flex', flexDirection: 'column', gap: 4 }}>
          Written reason — required
          <textarea
            value={reason}
            rows={4}
            aria-label="Written reason for the decision"
            aria-invalid={reasonError !== null}
            onChange={(event) => setReason(event.target.value)}
            style={{
              ...T_LABEL,
              fontFamily: 'var(--font-sans)',
              background: 'var(--color-canvas)',
              color: 'var(--color-ink)',
              border: `1px solid ${reasonError !== null ? 'var(--color-state-failed)' : 'var(--color-hairline-strong)'}`,
              borderRadius: 'var(--radius-control)',
              padding: 8,
              resize: 'vertical',
            }}
          />
        </label>

        {/* Tier 1 of the error ladder: the inline field error, naming the field. */}
        {reasonError !== null ? (
          <p
            role="alert"
            data-field-error="reason"
            style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}
          >
            {reasonError}
          </p>
        ) : null}
        {fields.map((field) => (
          <p
            key={field.location}
            role="alert"
            data-field-error={field.location}
            style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}
          >
            {field.location}: {field.message}
          </p>
        ))}

        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {choices.map((choice) => (
            <button
              key={choice.id}
              type="button"
              title={choice.hint}
              disabled={pending}
              data-deep-link-action={deepAction === choice.id ? 'preselected' : undefined}
              onClick={() => void onDecide(choice.id)}
              style={{
                ...CONTROL,
                flex: '1 1 auto',
                cursor: pending ? 'progress' : 'pointer',
                background: deepAction === choice.id ? 'var(--color-elev-2)' : 'transparent',
                borderColor: deepAction === choice.id ? 'var(--color-evidence)' : 'var(--color-hairline-strong)',
              }}
            >
              {choice.label}
            </button>
          ))}
        </div>

        {conflict !== null ? (
          /* Every word here is a field of the 409 the API sent: `current_version` and the
             stored row in `current` (apps/api/problems.py:196-208, decisions.py:803-813).
             The previous version printed `current_seq` / `decided_by` / `decided_at`,
             which this API has never sent, so the merge view showed two blanks and told
             the analyst to read a decision that was not on the screen. */
          <div data-merge-view style={{ ...PANEL_SUNKEN, padding: 10 }}>
            <p style={{ ...T_LABEL, color: 'var(--color-ink)', margin: 0 }}>
              {conflict.current === null
                ? 'This case has moved on since you loaded it, and the server sent no winning row to read.'
                : `Another decision was recorded while you were writing: ${conflict.current.action} #${count(
                    conflict.current.decision_seq,
                  )} by ${conflict.current.actor_id}.`}
            </p>
            {conflict.current !== null ? (
              <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>
                “{conflict.current.reason}”
              </p>
            ) : null}
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginTop: 4 }}>
              Your text is still in the box. Nothing was written and nothing was overwritten — retry against version{' '}
              {String(conflict.current_version)} once you have read theirs.
            </p>
            {conflict.current !== null ? (
              <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 4 }}>
                {conflict.current.four_eyes_state === 'pending'
                  ? 'that decision is held for a second reviewer, so its delivery has not been promised either'
                  : `four-eyes state: ${conflict.current.four_eyes_state}`}
              </p>
            ) : null}
          </div>
        ) : null}

        {failure !== null ? (
          <p role="alert" data-write-error style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}>
            Not written: {failureTitle(failure)} — {failureDetail(failure) ?? 'the server refused the decision'}. The
            history closed with the request, so it now shows only what is actually recorded.
          </p>
        ) : null}
        {confirmFailure !== null ? (
          <p role="alert" data-write-error style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}>
            Confirmation not recorded: {failureTitle(confirmFailure)} —{' '}
            {failureDetail(confirmFailure) ?? 'the second reviewer’s write was refused'}. Nothing was queued.
          </p>
        ) : null}

        {receipt !== null ? (
          <div data-decision-receipt style={{ ...PANEL_SUNKEN, padding: 10 }}>
            <p style={{ ...T_LABEL, color: 'var(--color-ink)', margin: 0 }}>
              {`Decision #${count(receipt.decision_seq)} written on case ${receipt.case_id}`}
            </p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginTop: 4 }}>
              chain #{count(receipt.chain_seq)} · version {String(receipt.case_version)} ·{' '}
              {receipt.decided_on_superseded_run
                ? 'decided on a superseded run, stamped as such'
                : 'decided on the pinned run'}
            </p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 4 }}>
              <code style={{ ...T_MONO, fontSize: '0.625rem' }}>{receipt.row_hash.slice(0, 24)}</code>
            </p>
          </div>
        ) : null}

        {/* The four-eyes gate, rendered only when the receipt says it is holding. The
            note is the second reviewer's own sentence — `confirmation_note` carries the
            same server-side minimum as a decision reason (schemas/case.py:296). */}
        {awaitingSecondReviewer ? (
          <div data-four-eyes style={{ ...PANEL_SUNKEN, padding: 10 }}>
            <p style={{ ...T_LABEL, color: 'var(--color-ink)', margin: 0 }}>
              Held for a second reviewer — nothing has been delivered
            </p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginTop: 4 }}>
              The row is chained and the case is at version {String(receipt?.case_version ?? 0)}, but the outbox carries
              no promise for it: {receipt?.outbox_queued === true ? 'delivery queued' : 'delivery not queued'}. A
              second, different reviewer confirming queues it.
            </p>
            <label style={{ ...T_LABEL, display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8 }}>
              Confirmation note — required
              <textarea
                value={note}
                rows={2}
                aria-label="Confirmation note for the second reviewer"
                onChange={(event) => setNote(event.target.value)}
                style={{
                  ...T_LABEL,
                  fontFamily: 'var(--font-sans)',
                  background: 'var(--color-canvas)',
                  color: 'var(--color-ink)',
                  border: '1px solid var(--color-hairline-strong)',
                  borderRadius: 'var(--radius-control)',
                  padding: 8,
                  resize: 'vertical',
                }}
              />
            </label>
            <button
              type="button"
              onClick={() => void onConfirm()}
              disabled={pending}
              style={{ ...CONTROL, marginTop: 8, cursor: pending ? 'progress' : 'pointer' }}
            >
              Confirm as second reviewer
            </button>
          </div>
        ) : null}

        <MoneyFigure
          figure={payload.header.economics.exposure}
          assumptions={assumptions}
          label="What is at stake"
          compact
          showBand={false}
        />
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
          {count(exposure.minor)} minor {exposure.currency} on this line · {count(payload.decisions.length)} decisions
          recorded · an empty reason is refused here and again by the server, because a decision nobody can explain is
          not an audit trail
        </p>
      </div>
    </Pane>
  );
}

function DecisionHistory({ decisions, timeZone }: { decisions: Decision[]; timeZone: string | null }): ReactElement {
  return (
    <>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '0 0 8px' }}>
        Append-only and hash-chained. A reversal is a new row that references the original, so both stay in the
        timeline: mutating a record is what would destroy the integrity claim.
      </p>
      {decisions.length === 0 ? (
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
          Nothing decided on this account yet. The rail above is the only way a row appears here.
        </p>
      ) : (
        <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {decisions.map((decision) => (
            <li key={`${String(decision.seq)}-${decision.hash}`} style={{ padding: '8px 0', ...HAIRLINE_BOTTOM }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <strong style={{ ...T_LABEL, fontWeight: 600, textTransform: 'capitalize', color: 'var(--color-ink)' }}>
                  {decision.decision}
                </strong>
                {/* A pending row has no sequence, no actor and no role: the server has
                    not answered yet, so nothing is printed in their place. */}
                {decision.pending === true ? null : (
                  <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                    #{count(decision.seq)} · {decision.actor} ({decision.role})
                  </span>
                )}
                {decision.pending === true ? (
                  <span data-pending-decision style={{ ...T_MICRO, color: 'var(--color-state-running)' }}>
                    not yet written
                  </span>
                ) : null}
                {decision.four_eyes_required ? (
                  <span style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>four-eyes required</span>
                ) : null}
              </div>
              <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: '4px 0', whiteSpace: 'pre-wrap' }}>
                {decision.reason}
              </p>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <span title="audit hash" style={{ color: 'var(--color-evidence)' }}>
                  <Icon name="hash-link" size={12} />
                </span>
                <code
                  data-hash-chip={decision.hash === 'pending' ? 'pending' : 'recorded'}
                  style={{
                    ...T_MONO,
                    fontSize: '0.625rem',
                    color: decision.hash === 'pending' ? 'var(--color-ink-faint)' : 'var(--color-ink)',
                    wordBreak: 'break-all',
                  }}
                >
                  {decision.hash === 'pending' ? 'awaiting the server’s chain entry' : decision.hash.slice(0, 24)}
                </code>
                {decision.reversible_of !== null ? (
                  <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                    reverses #{String(decision.reversible_of)}
                  </span>
                ) : null}
              </div>
              <div style={{ marginTop: 4 }}>
                <Timestamp
                  iso={decision.pending === true ? null : decision.recorded_at}
                  timeZone={timeZone}
                  sense="recorded"
                />
              </div>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}
