/* =============================================================================
   Model, backtest and validation — plan §14 P8b-7.

   Every chart here is served as data by the API and drawn by the same visx
   primitives as the rest of the product. Nothing is a screenshot of a notebook: a
   screenshot is a claim about a run nobody can interrogate, and the page that carries
   the ablation table is exactly where that would be fatal.

   Three things this page is required to say out loud, and does:
   * the walk-forward diagram shows the embargo gap as a gap, with the 30-day lookback
     it exists to protect named;
   * the risk-adjusted ratio is labelled as what it is, with the reason it is not a
     Sharpe ratio beside the formula;
   * graceful degradation is stated as a property, listing which ports were down for
     this run and which deterministic path answered instead.

   The limitations section is written in the first person, because a list of hedges in
   the passive voice reads as a disclaimer and a list in the first person reads as
   somebody who knows what they built.
   ============================================================================= */

'use client';

import { type CSSProperties, Fragment, type ReactElement } from 'react';

import { Pane } from '@/components/Pane';
import { BarWithLine, ChartFrame, LineChart, minimumSeriesNote } from '@/components/charts/charts';
import { glyphFor } from '@/components/typology';
import { BandBadge } from '@/components/ui/BandBadge';
import { Assumptions, Hairline, RunIdChip, Timestamp } from '@/components/ui/provenance';
import { GAP_TIGHT, HAIRLINE_BOTTOM, PANEL_SUNKEN, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';
import { Icon } from '@/design/icons/Icon';
import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { type AssumptionLine, ROUTES, type Validation } from '@/lib/api/contract';
import { type QueryState, useResource } from '@/lib/api/hooks';
import { failureDetail, failureRunId, failureTitle, isRunNotFound } from '@/lib/api/problem';
import { PIPELINE_COMMAND, RUNTIME_ESTIMATE_FALLBACK } from '@/lib/copy';
import { compactFromMinor, count, percent, ratio } from '@/lib/format/money';
import { formatDate } from '@/lib/format/time';

/* ---------------------------------------------------------------- geometry --

   ONE table of this page's pane geometry, read by the resolved tree and by its
   skeleton alike. `body` is the measured content height of that pane at the desktop
   breakpoint, and `Pane` applies it as a reserved min-height in both states, so the
   pending tree and the answered tree are the same height by construction rather than
   by a skeleton guessing at a layout it cannot see. The version that returned a
   two-pane stub and then swapped in thirteen panes measured 0.53 CLS — the largest
   shift in the app — because everything below the fold arrived with the answer. */
type PaneGeometry = {
  title: string;
  operation: string;
  columns: { key: string; width: string }[];
  rows: number;
  rowHeight?: number;
  body: number;
};

type PaneKey =
  | 'dataset'
  | 'folds'
  | 'baselines'
  | 'pr'
  | 'reliability'
  | 'confusion'
  | 'typologies'
  | 'ablation'
  | 'importance'
  | 'fairness'
  | 'tail'
  | 'degraded'
  | 'limitations';

const PANES: Record<PaneKey, PaneGeometry> = {
  dataset: {
    title: 'Dataset card',
    operation: 'Loading the dataset card',
    columns: [{ key: 'd', width: '100%' }],
    rows: 5,
    body: 309,
  },
  folds: {
    title: 'Walk-forward folds',
    operation: 'Loading the folds',
    columns: [{ key: 'f', width: '100%' }],
    rows: 5,
    body: 309,
  },
  baselines: {
    title: 'Baselines against the final system, per corpus',
    operation: 'Loading the comparison',
    columns: [
      { key: 'c', width: '12%' },
      { key: 'v', width: '24%' },
      { key: 'p', width: '16%' },
      { key: 'q', width: '16%' },
      { key: 'n', width: '16%' },
      { key: 'b', width: '16%' },
    ],
    rows: 6,
    body: 172,
  },
  pr: {
    title: 'Precision–recall, with the operating point',
    operation: 'Loading the PR curve',
    columns: [{ key: 'c', width: '100%' }],
    rows: 1,
    rowHeight: 284,
    body: 284,
  },
  reliability: {
    title: 'Reliability diagram',
    operation: 'Loading the reliability bins',
    columns: [{ key: 'c', width: '100%' }],
    rows: 1,
    rowHeight: 284,
    body: 284,
  },
  confusion: {
    title: 'Confusion at the budget',
    operation: 'Loading the confusion matrix',
    columns: [{ key: 'c', width: '100%' }],
    rows: 4,
    body: 224,
  },
  typologies: {
    title: 'Per-typology recall',
    operation: 'Loading typology recall',
    columns: [
      { key: 't', width: '60%' },
      { key: 'r', width: '20%' },
      { key: 's', width: '20%' },
    ],
    rows: 6,
    body: 224,
  },
  ablation: {
    title: 'Ablation',
    operation: 'Loading the ablation',
    columns: [
      { key: 'v', width: '26%' },
      { key: 'q', width: '30%' },
      { key: 'p', width: '14%' },
      { key: 'c', width: '14%' },
      { key: 'n', width: '16%' },
    ],
    rows: 5,
    body: 289,
  },
  importance: {
    title: 'Global SHAP importance',
    operation: 'Loading importances',
    columns: [
      { key: 'f', width: '60%' },
      { key: 'v', width: '40%' },
    ],
    rows: 8,
    body: 356,
  },
  fairness: {
    title: 'Fairness and perturbation',
    operation: 'Loading the fairness checks',
    columns: [
      { key: 'd', width: '30%' },
      { key: 'b', width: '40%' },
      { key: 'f', width: '30%' },
    ],
    rows: 8,
    body: 356,
  },
  tail: {
    title: 'Tail behaviour and seed stability',
    operation: 'Loading the tail metrics',
    columns: [{ key: 't', width: '100%' }],
    rows: 5,
    body: 322,
  },
  degraded: {
    title: 'Degradation this run',
    operation: 'Listing degraded dependencies',
    columns: [
      { key: 'n', width: '40%' },
      { key: 'f', width: '60%' },
    ],
    rows: 3,
    body: 322,
  },
  limitations: {
    title: 'Limitations',
    operation: 'Loading the limitations',
    columns: [{ key: 'l', width: '100%' }],
    rows: 5,
    body: 281,
  },
};

const PAGE: CSSProperties = {
  padding: 'var(--spacing-pane-gap)',
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--spacing-pane-gap)',
};
const HALF: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
  gap: 'var(--spacing-pane-gap)',
};

/**
 * The same thirteen panes, empty, while the run has not answered — plus the failure
 * itself once asking has stopped. A skeleton with no failure on it is a page that
 * claims it is still loading, which is the one lie a loading state is allowed not to
 * tell, so the tier-2 pane sits above the reserved geometry and retries on its own.
 */
function ModelSkeleton({
  failure,
  pending,
  onRetry,
  attempt,
  retrying,
}: {
  failure: QueryState<Validation>['failure'];
  /** Can the shared query still answer? When it cannot, no pane on this route is
   *  "loading" any more — each one says it has no answer, over the same geometry. */
  pending: boolean;
  onRetry: () => void;
  attempt: number;
  retrying: boolean;
}): ReactElement {
  const pane = (key: PaneKey) => (
    <Pane
      id={key}
      title={PANES[key].title}
      operation={PANES[key].operation}
      skeleton={{ columns: PANES[key].columns, rows: PANES[key].rows, rowHeight: PANES[key].rowHeight }}
      reserveHeight={PANES[key].body}
      pending={pending}
      onRetry={pending ? null : onRetry}
    >
      <span />
    </Pane>
  );
  return (
    <div style={PAGE}>
      {failure !== null ? (
        <ErrorPane
          paneId="validation"
          operation="Loading the validation record"
          error={{
            title: failureTitle(failure),
            detail: failureDetail(failure) ?? undefined,
            run_id: failureRunId(failure) ?? undefined,
          }}
          onRetry={onRetry}
          attempt={attempt}
          retrying={retrying}
        />
      ) : null}
      <div style={HALF}>
        {pane('dataset')}
        {pane('folds')}
      </div>
      {pane('baselines')}
      <div style={HALF}>
        {pane('pr')}
        {pane('reliability')}
      </div>
      <div style={HALF}>
        {pane('confusion')}
        {pane('typologies')}
      </div>
      {pane('ablation')}
      <div style={HALF}>
        {pane('importance')}
        {pane('fairness')}
      </div>
      <div style={HALF}>
        {pane('tail')}
        {pane('degraded')}
      </div>
      {pane('limitations')}
    </div>
  );
}

export default function ModelPage(): ReactElement {
  const validation = useResource('validation', ROUTES.validation.path, ROUTES.validation.data);
  const dataset = useResource('dataset', ROUTES.dataset.path, ROUTES.dataset.data);
  const runtime = useResource('runtime', ROUTES.runtime.path, ROUTES.runtime.data);
  const timeZone = runtime.data?.deployment_timezone ?? null;

  if (validation.data === null)
    return validation.failure !== null && isRunNotFound(validation.failure) ? (
      /* 2 813 px is the height of the thirteen-reserved-pane skeleton this swap
         replaces (measured at 1440×900). Swapping a 2 813 px tree for a 230 px
         panel is a 0.137 CLS event — the footer teleports into the viewport —
         so the empty state inherits the tree's box instead of collapsing it. */
      <div data-cls-anchor="model-empty" style={{ ...PAGE, minHeight: 2813 }}>
        <EmptyState kind="no-run" command={PIPELINE_COMMAND} expectedRuntime={RUNTIME_ESTIMATE_FALLBACK} />
      </div>
    ) : (
      <ModelSkeleton
        failure={validation.failure}
        pending={validation.isPending}
        onRetry={() => void validation.refetch()}
        attempt={validation.attempts}
        retrying={validation.isFetching}
      />
    );

  const data = validation.data;
  const assumptions = validation.meta?.assumptions ?? [];

  return (
    <div style={PAGE}>
      <div style={HALF}>
        {dataset.data === null ? (
          /* Its own query, its own tier. This pane is the one place on the route where a
             second request can fail while the thirteen validation panes above are live,
             so it carries the failure itself rather than a skeleton that would never
             stop — and it keeps its reserved geometry either way. */
          <Pane
            id="dataset"
            title={PANES.dataset.title}
            operation={PANES.dataset.operation}
            meta={dataset.meta}
            failure={dataset.failure}
            pending={dataset.isPending}
            onRetry={() => void dataset.refetch()}
            attempt={dataset.attempts}
            retrying={dataset.isFetching}
            skeleton={{ columns: PANES.dataset.columns, rows: PANES.dataset.rows }}
            reserveHeight={PANES.dataset.body}
          >
            <span />
          </Pane>
        ) : (
          <DatasetCard />
        )}

        <Pane
          id="folds"
          title={PANES.folds.title}
          operation={PANES.folds.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.folds.columns, rows: PANES.folds.rows }}
          reserveHeight={PANES.folds.body}
        >
          <Folds folds={data} timeZone={timeZone} />
        </Pane>
      </div>

      <Pane
        id="baselines"
        title={PANES.baselines.title}
        operation={PANES.baselines.operation}
        meta={validation.meta}
        skeleton={{ columns: PANES.baselines.columns, rows: PANES.baselines.rows }}
        reserveHeight={PANES.baselines.body}
      >
        <BaselineTable data={data} assumptions={assumptions} />
      </Pane>

      <div style={HALF}>
        <Pane
          id="pr"
          title={PANES.pr.title}
          operation={PANES.pr.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.pr.columns, rows: PANES.pr.rows, rowHeight: PANES.pr.rowHeight }}
          reserveHeight={PANES.pr.body}
        >
          <PrCurve data={data} />
        </Pane>
        <Pane
          id="reliability"
          title={PANES.reliability.title}
          operation={PANES.reliability.operation}
          meta={validation.meta}
          skeleton={{
            columns: PANES.reliability.columns,
            rows: PANES.reliability.rows,
            rowHeight: PANES.reliability.rowHeight,
          }}
          reserveHeight={PANES.reliability.body}
        >
          <Reliability data={data} />
        </Pane>
      </div>

      <div style={HALF}>
        <Pane
          id="confusion"
          title={PANES.confusion.title}
          operation={PANES.confusion.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.confusion.columns, rows: PANES.confusion.rows }}
          reserveHeight={PANES.confusion.body}
        >
          <Confusion data={data} />
        </Pane>
        <Pane
          id="typologies"
          title={PANES.typologies.title}
          operation={PANES.typologies.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.typologies.columns, rows: PANES.typologies.rows }}
          reserveHeight={PANES.typologies.body}
        >
          <TypologyRecall data={data} />
        </Pane>
      </div>

      {/* The ablation table. 00 §F: never cut. */}
      <Pane
        id="ablation"
        title={PANES.ablation.title}
        operation={PANES.ablation.operation}
        meta={validation.meta}
        skeleton={{ columns: PANES.ablation.columns, rows: PANES.ablation.rows }}
        reserveHeight={PANES.ablation.body}
      >
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--text-label)' }}>
          <thead>
            <tr>
              {['variant', 'question it answers', 'corpus', 'PR-AUC', '95% CI', 'net benefit'].map((heading) => (
                <th
                  key={heading}
                  style={{
                    textAlign: 'left',
                    padding: '4px 6px',
                    ...HAIRLINE_BOTTOM,
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
            {data.ablation.map((row) => (
              <tr key={`${row.corpus}-${row.variant}`} data-ablation-row={row.variant}>
                <td style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink)' }}>{row.variant}</td>
                <td style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}>
                  {row.question}
                </td>
                <td style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM, ...T_MONO, fontSize: 'var(--text-micro)' }}>
                  {row.corpus}
                </td>
                <td className="u-num" style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM }}>
                  {row.pr_auc.toFixed(3)}
                </td>
                <td
                  className="u-num"
                  style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}
                >
                  {row.ci.length === 2 ? `${row.ci[0]?.toFixed(3)} – ${row.ci[1]?.toFixed(3)}` : 'no interval recorded'}
                </td>
                <td className="u-num" style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM }}>
                  {compactFromMinor(row.net_benefit.value.minor, row.net_benefit.value.decimals)}{' '}
                  {row.net_benefit.value.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
          {count(data.configurations_evaluated)} configurations were evaluated during selection, so the best validation
          result above is optimistically biased — which is exactly why the headline comes from the untouched fold.
        </p>
        {/* The last column is money. Same rule as everywhere else in the product: a
            currency figure travels with the keys it was computed from. */}
        <Assumptions assumptions={assumptions} />
      </Pane>

      <div style={HALF}>
        <Pane
          id="importance"
          title={PANES.importance.title}
          operation={PANES.importance.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.importance.columns, rows: PANES.importance.rows }}
          reserveHeight={PANES.importance.body}
        >
          <BarWithLine
            rows={data.shap_importance.map((entry) => ({
              label: entry.label,
              value: entry.mean_abs,
              colour: 'var(--color-band-c)',
            }))}
            valueLabel="mean |SHAP|"
            formatValue={(value) => value.toFixed(3)}
            ariaLabel="Mean absolute SHAP value per feature"
          />
        </Pane>

        <Pane
          id="fairness"
          title={PANES.fairness.title}
          operation={PANES.fairness.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.fairness.columns, rows: PANES.fairness.rows }}
          reserveHeight={PANES.fairness.body}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Hairline label="false-positive rate by proxy dimension" />
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {data.fairness.map((entry) => (
                <li
                  key={`${entry.dimension}-${entry.bucket}`}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: 8,
                    padding: '3px 0',
                    ...HAIRLINE_BOTTOM,
                  }}
                >
                  <span style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
                    {entry.dimension} · {entry.bucket}
                  </span>
                  <span className="u-num" style={{ ...T_LABEL }}>
                    {percent(entry.false_positive_rate, 1)} · n={count(entry.n)}
                  </span>
                </li>
              ))}
            </ul>
            <Hairline label="perturbation checks" />
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {data.perturbations.map((entry) => (
                <li key={entry.name} style={{ padding: '3px 0', ...HAIRLINE_BOTTOM }}>
                  <span style={{ ...T_LABEL, color: 'var(--color-ink)' }}>{entry.name}</span>
                  <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                    {' '}
                    · {entry.measure} {ratio(entry.result, 3)}
                  </span>
                  {entry.note.length > 0 ? (
                    <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>{entry.note}</p>
                  ) : null}
                </li>
              ))}
            </ul>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              No protected attribute exists in either corpus, so this is a proxy check and is described as one.
            </p>
          </div>
        </Pane>
      </div>

      <div style={HALF}>
        <Pane
          id="tail"
          title={PANES.tail.title}
          operation={PANES.tail.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.tail.columns, rows: PANES.tail.rows }}
          reserveHeight={PANES.tail.body}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {data.drawdown.map((entry) => (
              <div key={entry.policy} style={{ ...PANEL_SUNKEN, padding: 10 }}>
                <p style={{ ...T_LABEL, margin: 0 }}>{entry.policy} · max drawdown</p>
                <p
                  className="u-num"
                  style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}
                >
                  {compactFromMinor(entry.value.minor, entry.value.decimals)} {entry.value.currency}
                </p>
                {entry.zero_because !== null ? (
                  <p style={{ ...T_MICRO, color: 'var(--color-state-done)', margin: 0 }}>{entry.zero_because}</p>
                ) : null}
              </div>
            ))}
            <div style={{ ...PANEL_SUNKEN, padding: 10 }} data-risk-adjusted>
              <p style={{ ...T_LABEL, margin: 0 }}>Risk-adjusted benefit ratio</p>
              <p className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
                {ratio(data.risk_adjusted.value, 2)}
              </p>
              <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>{data.risk_adjusted.formula}</p>
              <p style={{ ...T_MICRO, color: 'var(--color-state-running)', margin: '4px 0 0' }}>
                explicitly not a Sharpe ratio: {data.risk_adjusted.not_sharpe_because}
              </p>
            </div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
              {String(data.seeds.count)} seeds · {data.seeds.metric} {ratio(data.seeds.mean, 3)} ±{' '}
              {ratio(data.seeds.sd, 3)} — reported as a spread, not as the lucky run
            </p>
            <Assumptions assumptions={assumptions} />
          </div>
        </Pane>

        <Pane
          id="degraded"
          title={PANES.degraded.title}
          operation={PANES.degraded.operation}
          meta={validation.meta}
          skeleton={{ columns: PANES.degraded.columns, rows: PANES.degraded.rows }}
          reserveHeight={PANES.degraded.body}
        >
          {data.degraded_dependencies.length === 0 ? (
            <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
              Every dependency answered for this run. When one does not, it appears here with the deterministic path
              that replaced it, and the affected pane keeps working.
            </p>
          ) : (
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {data.degraded_dependencies.map((entry) => (
                <li
                  key={entry.name}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
                    gap: 8,
                    padding: '5px 0',
                    ...HAIRLINE_BOTTOM,
                  }}
                >
                  <span style={{ ...T_LABEL, color: 'var(--color-ink)' }}>{entry.name}</span>
                  <span style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>fallback: {entry.fallback}</span>
                </li>
              ))}
            </ul>
          )}
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
            graceful degradation under partial failure is a design requirement here, not an accident of the demo: a dead
            solver or a missing tracking store changes what is claimed, never whether the screen works.
          </p>
        </Pane>
      </div>

      {/* Limitations, first person, never cut. */}
      <Pane
        id="limitations"
        title={PANES.limitations.title}
        operation={PANES.limitations.operation}
        meta={validation.meta}
        skeleton={{ columns: PANES.limitations.columns, rows: PANES.limitations.rows }}
        reserveHeight={PANES.limitations.body}
      >
        <ol
          data-limitations
          style={{ margin: 0, padding: '0 0 0 18px', display: 'flex', flexDirection: 'column', gap: 8 }}
        >
          {data.limitations.map((entry, index) => (
            <li key={index} style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '84ch' }}>
              {entry}
            </li>
          ))}
        </ol>
      </Pane>
    </div>
  );

  function DatasetCard(): ReactElement {
    const card = dataset.data;
    if (card === null) return <span />;
    return (
      <Pane
        id="dataset"
        title={PANES.dataset.title}
        operation={PANES.dataset.operation}
        meta={dataset.meta}
        skeleton={{ columns: PANES.dataset.columns, rows: PANES.dataset.rows }}
        reserveHeight={PANES.dataset.body}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <p style={{ ...T_LABEL, color: 'var(--color-ink)', fontWeight: 600 }}>{card.name}</p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
            licence {card.licence} · {card.licence_note}
          </p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>{card.citation}</p>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {card.files.map((file) => (
              <li
                key={file.filename}
                style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '3px 0', ...HAIRLINE_BOTTOM }}
              >
                <span style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink)' }}>
                  {file.filename}
                </span>
                <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginLeft: 'auto' }}>
                  {count(file.rows)} rows · {count(file.bytes)} bytes
                </span>
              </li>
            ))}
          </ul>
          <p
            data-sha
            style={{ ...T_MICRO, color: 'var(--color-ink-faint)', ...GAP_TIGHT, display: 'flex', flexWrap: 'wrap' }}
          >
            {card.files.map((file) => (
              <code key={file.sha256} style={{ ...T_MONO, fontSize: '0.625rem' }}>
                {file.sha256.slice(0, 16)}…{' '}
              </code>
            ))}
          </p>
          <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>{card.label_definition}</p>
          <ul style={{ margin: 0, padding: '0 0 0 18px' }}>
            {card.label_limits.map((entry) => (
              <li key={entry} style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                {entry}
              </li>
            ))}
          </ul>
          {card.sampling_rule !== null ? (
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>{card.sampling_rule}</p>
          ) : null}
          <Assumptions
            assumptions={assumptions}
            source={`retrieved ${formatDate(card.retrieved_at, timeZone ?? '')}`}
          />
          <RunIdChip runId={validation.meta?.run_id ?? null} traceId={validation.meta?.trace_id ?? null} />
        </div>
      </Pane>
    );
  }
}

/* --------------------------------------------------------------- pieces --- */

function Folds({ folds, timeZone }: { folds: Validation; timeZone: string | null }): ReactElement {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {folds.folds.map((fold) => (
        <div
          key={fold.index}
          data-fold={fold.index}
          style={{ display: 'grid', gridTemplateColumns: '36px minmax(0,1fr)', gap: 8, alignItems: 'start' }}
        >
          <span className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink-faint)' }}>
            f{String(fold.index)}
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {/* train | embargo gap | test, drawn to a shared scale so the gap is visible
                as a gap rather than described in a caption. */}
            <div
              style={{ display: 'flex', height: 14, borderRadius: 'var(--radius-cell)', overflow: 'hidden' }}
              aria-hidden="true"
            >
              <span style={{ flex: 3, background: 'var(--color-band-a)' }} />
              <span
                style={{
                  width: 14,
                  background: 'var(--color-canvas-sunken)',
                  borderLeft: '1px solid var(--color-hairline-strong)',
                  borderRight: '1px solid var(--color-hairline-strong)',
                }}
              />
              <span style={{ flex: 1, background: 'var(--color-band-e)' }} />
            </div>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
              train {formatDate(fold.train_from, timeZone ?? '')} → {formatDate(fold.train_to, timeZone ?? '')} ·
              embargo {String(fold.embargo_days)} d · test {formatDate(fold.test_from, timeZone ?? '')} →{' '}
              {formatDate(fold.test_to, timeZone ?? '')} · {count(fold.positives)} positives
            </p>
            {fold.skipped_reason !== null ? (
              <p style={{ ...T_MICRO, color: 'var(--color-band-d)', margin: 0 }}>
                <Icon name="embargo" size={12} /> {fold.skipped_reason}
              </p>
            ) : null}
          </div>
        </div>
      ))}
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '76ch' }}>
        The embargo equals the longest feature lookback, so a rolling window cannot see across it into training data.
        {folds.optimised_on}. {folds.entity_disjoint_note}
      </p>
      {folds.test_touched_at !== null ? (
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
          test fold touched once, at <Timestamp iso={folds.test_touched_at} timeZone={timeZone} />
        </p>
      ) : null}
    </div>
  );
}

function BaselineTable({
  data,
  assumptions,
}: { data: Validation; assumptions: readonly AssumptionLine[] }): ReactElement {
  return (
    <>
      <div className="u-scroll" style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--text-label)', minWidth: 700 }}>
          <thead>
            <tr>
              {[
                'corpus',
                'variant',
                'PR-AUC',
                '95% CI',
                'precision at budget',
                'recall at budget',
                'net benefit',
                'per analyst-hour',
              ].map((heading) => (
                <th
                  key={heading}
                  style={{
                    textAlign: 'left',
                    padding: '4px 6px',
                    ...HAIRLINE_BOTTOM,
                    color: 'var(--color-ink-faint)',
                    fontWeight: 500,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {heading}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.baseline_table.map((row) => (
              <tr key={`${row.corpus}-${row.variant}`} data-baseline={row.variant}>
                <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, ...T_MONO, fontSize: 'var(--text-micro)' }}>
                  {row.corpus}
                </td>
                <td
                  style={{
                    padding: '4px 6px',
                    ...HAIRLINE_BOTTOM,
                    color: row.is_final ? 'var(--color-ink)' : 'var(--color-ink-muted)',
                    fontWeight: row.is_final ? 600 : 400,
                  }}
                >
                  {row.variant}
                </td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {row.pr_auc.toFixed(3)}
                </td>
                <td
                  className="u-num"
                  style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-faint)' }}
                >
                  {row.pr_auc_ci.length === 2
                    ? `${row.pr_auc_ci[0]?.toFixed(3)} – ${row.pr_auc_ci[1]?.toFixed(3)}`
                    : '—'}
                </td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {percent(row.precision_at_budget, 1)}
                </td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {percent(row.recall_at_budget, 1)}
                </td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {compactFromMinor(row.net_benefit.value.minor, row.net_benefit.value.decimals)}
                </td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {compactFromMinor(
                    row.benefit_per_analyst_hour.value.minor,
                    row.benefit_per_analyst_hour.value.decimals,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
        reported per corpus and never averaged into one headline: PaySim and IBM-AML label different behaviours, and a
        mean across the two would be a number about nothing.
      </p>
      {/* Two of these columns are money, so the pane names the assumptions they are a
          function of rather than leaving a currency figure to be read unlabelled. */}
      <Assumptions assumptions={assumptions} />
    </>
  );
}

function PrCurve({ data }: { data: Validation }): ReactElement {
  const byCorpus = new Map<string, { x: string; y: number; label: string | null }[]>();
  for (const point of data.pr_curve) {
    const list = byCorpus.get(point.corpus) ?? [];
    list.push({ x: String(point.recall), y: point.precision, label: null });
    byCorpus.set(point.corpus, list);
  }
  const series = [...byCorpus.entries()].map(([corpus, points]) => ({
    label: corpus,
    points: points.map((point, index) => ({
      ...point,
      x: new Date(Date.UTC(1970, 0, 1 + index)).toISOString(),
      y: point.y,
    })),
    is_policy: corpus === data.operating_point.corpus,
  }));
  return (
    <div>
      <LineChart
        points={series[0]?.points ?? []}
        formatY={(value) => value.toFixed(2)}
        formatX={() => ''}
        ariaLabel={`Precision-recall curve for ${series[0]?.label ?? 'the run'}`}
        yIsMoney={false}
      />
      {series.map((entry) => (
        <p key={entry.label} style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: '4px 0 0' }}>
          {entry.label}: {count(entry.points.length)} points · operating point{' '}
          {entry.label === data.operating_point.corpus
            ? `precision ${ratio(data.operating_point.precision, 3)} at recall ${ratio(data.operating_point.recall, 3)} (${data.operating_point.budget_label})`
            : 'not marked on this curve'}
        </p>
      ))}
      {minimumSeriesNote(series[0]?.points.length ?? 0, 'line') !== null ? (
        <ChartFrame
          height={60}
          note="The PR curve came back with fewer than two points, so no curve is drawn."
          label="pr curve guard"
        />
      ) : null}
    </div>
  );
}

function Reliability({ data }: { data: Validation }): ReactElement {
  return (
    <div>
      <BarWithLine
        rows={data.reliability.map((entry) => ({
          label: entry.bin,
          value: entry.predicted,
          secondary: entry.observed,
          colour: 'var(--color-band-b)',
        }))}
        valueLabel="predicted"
        secondaryLabel="observed"
        formatValue={(value) => value.toFixed(2)}
        ariaLabel="Predicted against observed rate, per calibration bin"
      />
      <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginTop: 6 }}>
        Brier {ratio(data.brier, 4)} · method {data.calibration_floor.method} · floor{' '}
        {count(data.calibration_floor.min_positives)} validation positives · calibration refused:{' '}
        {data.calibration_floor.refused ? 'yes, and the UI says probabilities are uncalibrated' : 'no'}
      </p>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '72ch' }}>
        Calibration is load-bearing rather than cosmetic here: the economics layer multiplies these probabilities by
        money, so a miscalibrated model produces wrong currency figures everywhere the queue is priced.
      </p>
    </div>
  );
}

function Confusion({ data }: { data: Validation }): ReactElement {
  const { confusion } = data;
  const cells = [
    { key: 'tp', label: 'caught', value: confusion.tp, band: 'C' as const },
    { key: 'fn', label: 'missed', value: confusion.fn, band: 'E' as const },
    { key: 'fp', label: 'wrongly touched', value: confusion.fp, band: 'D' as const },
    { key: 'tn', label: 'correctly left alone', value: confusion.tn, band: 'A' as const },
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {cells.map((cell) => (
          <div key={cell.key} style={{ ...PANEL_SUNKEN, padding: 10 }}>
            <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: 0 }}>{cell.label}</p>
            <p className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
              {count(cell.value)}
            </p>
            <BandBadge band={cell.band} describe={false} size={11} />
          </div>
        ))}
      </div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        at {confusion.budget_label}.{' '}
        {confusion.precision_undefined
          ? 'precision is undefined here: the policy raised no alerts above the cutoff, and reporting it as zero or one would be a fabrication.'
          : 'precision is defined because the budget raised alerts.'}
      </p>
    </div>
  );
}

function TypologyRecall({ data }: { data: Validation }): ReactElement {
  return (
    <Fragment>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {data.typology_recall.map((entry) => (
          <li
            key={entry.rule_code}
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0,1fr) 70px 70px',
              gap: 8,
              alignItems: 'center',
              padding: '5px 0',
              ...HAIRLINE_BOTTOM,
            }}
          >
            <span style={{ ...T_LABEL, color: 'var(--color-ink)' }}>
              <Icon name={glyphFor(entry.typology)} size={14} /> {entry.rule_code}
            </span>
            <span className="u-num" style={{ ...T_LABEL }}>
              {percent(entry.recall, 1)}
            </span>
            <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              n={count(entry.support)}
            </span>
          </li>
        ))}
      </ul>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 6 }}>
        per typology rather than pooled: a model can have good aggregate recall and no cycle detection at all, and the
        pooled number hides it.
      </p>
    </Fragment>
  );
}
