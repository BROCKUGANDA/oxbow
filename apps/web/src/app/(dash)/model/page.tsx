/* =============================================================================
   Model, backtest and validation — plan §14 P8b-7.

   Every chart here is served as data by the API and drawn by the same visx
   primitives as the rest of the product. Nothing is a screenshot of a notebook: a
   screenshot is a claim about a run nobody can interrogate, and the page that carries
   the ablation table is exactly where that would be fatal.

   WHAT THIS PAGE READS. `ValidationDecoder` decodes `ValidationBundle`
   (`apps/api/schemas/validation.py:231-254`) exactly as the model declares it and
   nothing else: `run_id`, `corpora`, `folds` (FoldRow), `ablation` (AblationRowView),
   `curves` (CurveSeries), `confusion` (ConfusionMatrixView | null), `fairness`
   (FairnessAxisView), `perturbations`, `metrics`, `typology_recall`, `overfitting`,
   `label_quality`, `limitations`, `assumptions`. It used to demand fourteen names the
   route has never sent (`baseline_table`, `pr_curve`, `operating_point`, `reliability`,
   `brier`, `calibration_floor`, `shap_importance`, `drawdown`, `risk_adjusted`, `seeds`,
   `configurations_evaluated`, `test_touched_at`, `degraded_dependencies`,
   `entity_disjoint_note`), so the decode threw, `validation.data` stayed null and all
   thirteen panes sat in their skeletons over a 200 response.

   Three things this page is required to say out loud, and does:
   * the walk-forward diagram shows the embargo gap as a gap, and the gap is the served
     `embargo_days` with the served `embargo_end` beside it, not a decorative spacer;
   * the risk-adjusted ratio is labelled with the served note the run stored — the
     `label = formula; is_sharpe_ratio=…` line that arrives on the metric itself, since
     plan §12 requires the label and the formula on the page and the server carries both;
   * graceful degradation is stated from `meta.degraded` and `meta.degraded_reason`,
     which is where the API actually puts it.

   Where a pane's old content had no served equivalent — the baseline-vs-final table,
   the calibration floor and its refusal, the seed-stability spread, the per-dependency
   fallback list — the pane renders what IS served and the gap is named once in
   apps/web/CONTRACT-GAPS.md. No branch here survives for a field the response model
   does not declare.

   The limitations section is written in the first person, because a list of hedges in
   the passive voice reads as a disclaimer and a list in the first person reads as
   somebody who knows what they built. Those sentences are served in `limitations` —
   echoed, never re-authored here, which is the same rule `routers/validation.py`
   states for itself.
   ============================================================================= */

'use client';

import { type CSSProperties, Fragment, type ReactElement } from 'react';

import { Pane } from '@/components/Pane';
import { BarWithLine, ChartFrame, minimumSeriesNote } from '@/components/charts/charts';
import { Assumptions, Hairline, RunIdChip, Timestamp } from '@/components/ui/provenance';
import { GAP_TIGHT, HAIRLINE_BOTTOM, PANEL_SUNKEN, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';
import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import {
  type AssumptionLine,
  type CurveSeries,
  type DatasetCard,
  type ListMeta,
  type Money,
  ROUTES,
  type Validation,
  type ValidationFold,
  type ValidationMetric,
} from '@/lib/api/contract';
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
  | 'metrics'
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
    title: 'Dataset cards',
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
  metrics: {
    title: 'Run metrics as stored, per corpus',
    operation: 'Loading the stored metrics',
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
    title: 'Tail figures and Monte Carlo settings',
    operation: 'Loading the tail metrics',
    columns: [{ key: 't', width: '100%' }],
    rows: 5,
    body: 322,
  },
  degraded: {
    title: 'Degradation this run',
    operation: 'Reading the run’s degraded state',
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
      resolved={false}
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
      {pane('metrics')}
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
  /* The bundle carries its own assumption lines (`ValidationBundle.assumptions`, set to
     the economics source by routers/validation.py:156-163) and the envelope carries the
     run's. The payload's own list wins because it names the file the money figures on
     THIS page are a function of; the meta list is the fallback for a run that stored
     none, and an empty list is refused by the Assumptions component rather than hidden. */
  const assumptions = data.assumptions.length > 0 ? data.assumptions : (validation.meta?.assumptions ?? []);

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
            resolved={false}
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
          <DatasetCardPane card={dataset.data} meta={dataset.meta} failure={dataset.failure} />
        )}

        <Pane
          id="folds"
          title={PANES.folds.title}
          operation={PANES.folds.operation}
          meta={validation.meta}
          resolved
          skeleton={{ columns: PANES.folds.columns, rows: PANES.folds.rows }}
          reserveHeight={PANES.folds.body}
        >
          <Folds data={data} timeZone={timeZone} assumptions={assumptions} />
        </Pane>
      </div>

      <Pane
        id="metrics"
        title={PANES.metrics.title}
        operation={PANES.metrics.operation}
        meta={validation.meta}
        resolved
        skeleton={{ columns: PANES.metrics.columns, rows: PANES.metrics.rows }}
        reserveHeight={PANES.metrics.body}
      >
        <MetricTable metrics={data.metrics} corpora={data.corpora} />
      </Pane>

      <div style={HALF}>
        <Pane
          id="pr"
          title={PANES.pr.title}
          operation={PANES.pr.operation}
          meta={validation.meta}
          resolved
          skeleton={{ columns: PANES.pr.columns, rows: PANES.pr.rows, rowHeight: PANES.pr.rowHeight }}
          reserveHeight={PANES.pr.body}
        >
          <CurvePane family="pr_curve" curves={data.curves} />
        </Pane>
        <Pane
          id="reliability"
          title={PANES.reliability.title}
          operation={PANES.reliability.operation}
          meta={validation.meta}
          resolved
          skeleton={{
            columns: PANES.reliability.columns,
            rows: PANES.reliability.rows,
            rowHeight: PANES.reliability.rowHeight,
          }}
          reserveHeight={PANES.reliability.body}
        >
          <CurvePane
            family="reliability"
            curves={data.curves}
            /* Reliability is the one family where x and y are both rates — predicted and
               observed — so the served pair is drawn as bar against line with the axis
               labels the series carries. */
          />
        </Pane>
      </div>

      <div style={HALF}>
        <Pane
          id="confusion"
          title={PANES.confusion.title}
          operation={PANES.confusion.operation}
          meta={validation.meta}
          resolved
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
          resolved
          skeleton={{ columns: PANES.typologies.columns, rows: PANES.typologies.rows }}
          reserveHeight={PANES.typologies.body}
        >
          <TypologyRecall metrics={data.typology_recall} />
        </Pane>
      </div>

      {/* The ablation table. 00 §F: never cut. */}
      <Pane
        id="ablation"
        title={PANES.ablation.title}
        operation={PANES.ablation.operation}
        meta={validation.meta}
        resolved
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
                <td style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink)' }}>
                  {row.variant}
                  {/* The three thesis marks are the server's reading of the variant name
                      (routers/validation.py:375-390), not this component's, so the row can
                      say which argument it carries. */}
                  {row.is_leakage_control ? <Tag tone="failed">leakage control</Tag> : null}
                  {row.is_graph_thesis ? <Tag tone="thesis">graph thesis</Tag> : null}
                  {row.is_pricing_thesis ? <Tag tone="thesis">pricing thesis</Tag> : null}
                </td>
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
                  {ratio(row.ci_low, 3)} – {ratio(row.ci_high, 3)}
                  <span style={{ color: 'var(--color-ink-faint)' }}>
                    {' '}
                    ({row.ci_method}, {count(row.n_resamples)} resamples, seed {String(row.seed)})
                  </span>
                </td>
                <td className="u-num" style={{ padding: '5px 6px', ...HAIRLINE_BOTTOM }}>
                  {compactFromMinor(row.net_benefit.minor, row.net_benefit.decimals)} {row.net_benefit.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.ablation.length === 0 ? (
          <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', marginTop: 8 }}>
            This run stored no ablation rows, so there is no arm table to read. The absence is stated rather than drawn
            as an empty grid, because an empty ablation pane on a rigour page reads as a result.
          </p>
        ) : null}
        <OverfittingLine data={data} />
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
          resolved
          skeleton={{ columns: PANES.importance.columns, rows: PANES.importance.rows }}
          reserveHeight={PANES.importance.body}
        >
          <CurvePane family="shap_global" curves={data.curves} />
        </Pane>

        <Pane
          id="fairness"
          title={PANES.fairness.title}
          operation={PANES.fairness.operation}
          meta={validation.meta}
          resolved
          skeleton={{ columns: PANES.fairness.columns, rows: PANES.fairness.rows }}
          reserveHeight={PANES.fairness.body}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {data.fairness.map((axis) => (
              <Fragment key={axis.axis}>
                <Hairline label={`${axis.axis} — false-positive and miss rate by bucket`} />
                <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0, maxWidth: '76ch' }}>
                  {axis.axis_rationale}
                </p>
                <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                  {axis.rows.map((row) => (
                    <li
                      key={`${axis.axis}-${row.bucket}`}
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: 8,
                        padding: '3px 0',
                        ...HAIRLINE_BOTTOM,
                      }}
                    >
                      <span style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>{row.bucket}</span>
                      <span className="u-num" style={{ ...T_LABEL }}>
                        FP {percent(row.fp_rate, 1)} ·{' '}
                        {row.fn_rate === null ? 'miss rate not measured' : `FN ${percent(row.fn_rate, 1)}`} · n=
                        {count(row.n)}
                      </span>
                    </li>
                  ))}
                  {axis.rows.length === 0 ? (
                    <li style={{ ...T_MICRO, color: 'var(--color-ink-faint)', padding: '3px 0' }}>
                      no buckets stored on this axis
                    </li>
                  ) : null}
                </ul>
              </Fragment>
            ))}
            {data.fairness.length === 0 ? (
              <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
                No fairness rows were stored for this run, so there is no proxy axis to report. An empty axis is not a
                clean result.
              </p>
            ) : null}
            <Hairline label="perturbation checks" />
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {data.perturbations.map((entry) => (
                <li key={`${entry.kind}-${String(entry.seed)}`} style={{ padding: '3px 0', ...HAIRLINE_BOTTOM }}>
                  <span style={{ ...T_LABEL, color: 'var(--color-ink)' }}>{entry.kind}</span>
                  <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                    {' '}
                    · magnitude {ratio(entry.magnitude, 3)} · result {ratio(entry.result, 3)} {entry.unit} · seed{' '}
                    {String(entry.seed)}
                  </span>
                  {entry.note.length > 0 ? (
                    <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>{entry.note}</p>
                  ) : null}
                </li>
              ))}
            </ul>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              No protected attribute exists in either corpus, so each axis above carries the rationale the run stored
              for using it as a proxy.
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
          resolved
          skeleton={{ columns: PANES.tail.columns, rows: PANES.tail.rows }}
          reserveHeight={PANES.tail.body}
        >
          <Tail data={data} assumptions={assumptions} />
        </Pane>

        <Pane
          id="degraded"
          title={PANES.degraded.title}
          operation={PANES.degraded.operation}
          meta={validation.meta}
          resolved
          skeleton={{ columns: PANES.degraded.columns, rows: PANES.degraded.rows }}
          reserveHeight={PANES.degraded.body}
        >
          <Degradation meta={validation.meta} />
        </Pane>
      </div>

      {/* Limitations, first person, never cut. */}
      <Pane
        id="limitations"
        title={PANES.limitations.title}
        operation={PANES.limitations.operation}
        meta={validation.meta}
        resolved
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
          {data.limitations.length === 0 ? (
            <li style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '84ch' }}>
              This run stored no limitation lines. The route echoes the limitations the artifact carries rather than
              writing its own, so an empty list here means the artifact declared none — which is itself a thing to ask
              about, not a clean bill.
            </li>
          ) : null}
        </ol>
        <Hairline label="Label quality, as the run states it" />
        <ul style={{ listStyle: 'none', margin: '6px 0 0', padding: 0 }}>
          {data.label_quality.entries.map((entry) => (
            <li
              key={entry.name}
              style={{ display: 'flex', justifyContent: 'space-between', gap: 8, padding: '3px 0', ...HAIRLINE_BOTTOM }}
            >
              <span style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
                {entry.name} · {entry.meaning}{' '}
                <span style={{ ...T_MONO, fontSize: 'var(--text-micro)' }}>{entry.corpus}</span>
              </span>
              <span className="u-num" style={{ ...T_LABEL }}>
                {ratio(entry.value, 4)}
              </span>
            </li>
          ))}
        </ul>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '84ch', marginTop: 6 }}>
          {data.label_quality.note}
        </p>
      </Pane>
    </div>
  );
}

/* --------------------------------------------------------------- pieces --- */

function Tag({ children, tone }: { children: string; tone: 'failed' | 'thesis' }): ReactElement {
  return (
    <span
      style={{
        ...T_MICRO,
        marginLeft: 6,
        padding: '1px 5px',
        borderRadius: 'var(--radius-control)',
        border: '1px solid var(--color-hairline-strong)',
        color: tone === 'failed' ? 'var(--color-state-failed)' : 'var(--color-evidence)',
      }}
    >
      {children}
    </span>
  );
}

function DatasetCardPane({
  card,
  meta,
  failure,
}: {
  card: DatasetCard;
  meta: ListMeta | null;
  failure: QueryState<unknown>['failure'];
}): ReactElement {
  return (
    <Pane
      id="dataset"
      title={PANES.dataset.title}
      operation={PANES.dataset.operation}
      meta={meta}
      resolved
      failure={failure}
      pending={false}
      skeleton={{ columns: PANES.dataset.columns, rows: PANES.dataset.rows }}
      reserveHeight={PANES.dataset.body}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {card.sources.map((source) => (
          <div
            key={source.source_id}
            data-dataset-source={source.source_id}
            style={{ display: 'flex', flexDirection: 'column', gap: 4, ...HAIRLINE_BOTTOM, paddingBottom: 8 }}
          >
            <p style={{ ...T_LABEL, color: 'var(--color-ink)', fontWeight: 600, margin: 0 }}>
              {source.name} <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>· {source.role}</span>
            </p>
            {/* The two attribution lines the licence clause of 01 §A rule 7 asks for,
                printed from the served fields: the licence and its obligation. */}
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
              licence {source.license} · obligation {source.license_obligation}
            </p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>{source.citation}</p>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
              {source.source_url} · retrieved {source.retrieval}
              {source.retrieved_at === null ? ' · no retrieval date recorded' : ` · ${source.retrieved_at}`}
            </p>
            <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: 0 }}>{source.description}</p>
            <p style={{ ...T_MICRO, color: 'var(--color-band-d)', margin: 0 }}>label caveat: {source.label_caveat}</p>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {source.files.map((file) => (
                <li
                  key={file.file_name}
                  style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '3px 0', ...HAIRLINE_BOTTOM }}
                >
                  <span style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink)' }}>
                    {file.file_name}
                  </span>
                  <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginLeft: 'auto' }}>
                    {file.row_count === null ? 'row count not measured' : `${count(file.row_count)} rows`} ·{' '}
                    {file.size_bytes === null ? 'size not measured' : `${count(file.size_bytes)} bytes`}
                  </span>
                </li>
              ))}
            </ul>
            <p
              data-sha
              style={{ ...T_MICRO, color: 'var(--color-ink-faint)', ...GAP_TIGHT, display: 'flex', flexWrap: 'wrap' }}
            >
              {source.files.map((file) => (
                <code key={file.sha256} style={{ ...T_MONO, fontSize: '0.625rem' }}>
                  {file.sha256.slice(0, 16)}…{' '}
                </code>
              ))}
            </p>
            {source.known_biases.length > 0 ? (
              <ul style={{ margin: 0, padding: '0 0 0 18px' }}>
                {source.known_biases.map((entry) => (
                  <li key={entry} style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                    {entry}
                  </li>
                ))}
              </ul>
            ) : null}
            {source.synthetic_fields.length > 0 ? (
              <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
                synthetic fields: {source.synthetic_fields.join(', ')}
              </p>
            ) : null}
          </div>
        ))}

        {card.refused_sources.length > 0 ? (
          <div>
            <Hairline label="Declared and refused" />
            <ul style={{ listStyle: 'none', margin: '6px 0 0', padding: 0 }}>
              {card.refused_sources.map((entry) => (
                <li key={entry.source_id} style={{ padding: '3px 0', ...HAIRLINE_BOTTOM }}>
                  <span style={{ ...T_LABEL, color: 'var(--color-ink)' }}>{entry.name}</span>
                  <span style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                    {' '}
                    · {entry.status} · {entry.reason}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {card.measurements.length > 0 ? (
          <div>
            <Hairline label="Measured on the corpus, with its command" />
            <ul style={{ listStyle: 'none', margin: '6px 0 0', padding: 0 }}>
              {card.measurements.map((entry) => (
                <li
                  key={`${entry.scope}-${entry.name}`}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    gap: 8,
                    padding: '3px 0',
                    ...HAIRLINE_BOTTOM,
                  }}
                >
                  <span style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                    {entry.scope} · {entry.name}
                    <span style={{ color: 'var(--color-ink-faint)' }}> ({entry.command})</span>
                  </span>
                  <span className="u-num" style={{ ...T_LABEL }}>
                    {ratio(entry.value, 4)}
                    {entry.unit === null ? '' : ` ${entry.unit}`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
            No corpus measurements are stored for this run, so none is shown: a statistic with no provenance is the kind
            of number a judge asks about last.
          </p>
        )}

        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
          sampling:{' '}
          {Object.entries(card.sampling)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join(' · ')}
        </p>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0, maxWidth: '80ch' }}>
          {card.deidentification.note}
        </p>
        <Assumptions assumptions={meta?.assumptions ?? []} />
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>{card.disclaimer}</p>
      </div>
    </Pane>
  );
}

function Folds({
  data,
  timeZone,
  assumptions,
}: {
  data: Validation;
  timeZone: string | null;
  assumptions: readonly AssumptionLine[];
}): ReactElement {
  const zone = timeZone ?? '';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
        corpora in this bundle: {data.corpora.length === 0 ? 'none named' : data.corpora.join(', ')} ·{' '}
        {count(data.folds.length)} folds
      </p>
      {data.folds.map((fold) => (
        <Fold key={`${fold.corpus}-${String(fold.fold_index)}`} fold={fold} zone={zone} />
      ))}
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '76ch' }}>
        The embargo equals the longest feature lookback, so a rolling window cannot see across it into training data.
        Entity overlap is reported per fold above, as the run stored it, rather than as one sentence over the table.
      </p>
      <Assumptions assumptions={assumptions} />
      <RunIdChip runId={data.run_id} traceId={null} />
    </div>
  );
}

function Fold({ fold, zone }: { fold: ValidationFold; zone: string }): ReactElement {
  return (
    <div
      data-fold={fold.fold_index}
      style={{ display: 'grid', gridTemplateColumns: '36px minmax(0,1fr)', gap: 8, alignItems: 'start' }}
    >
      <span className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink-faint)' }}>
        f{String(fold.fold_index)}
      </span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {/* train | embargo gap | test, drawn to a shared scale so the gap is visible
            as a gap rather than described in a caption. The gap is the served
            embargo_days; its width on screen is geometry, not a measurement. */}
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
          {fold.corpus} · train {formatDate(fold.train_start, zone)} → {formatDate(fold.train_end, zone)} · embargo{' '}
          {String(fold.embargo_days)} d (ends {formatDate(fold.embargo_end, zone)}) · test{' '}
          {formatDate(fold.test_start, zone)} → {formatDate(fold.test_end, zone)} · n={count(fold.n_train)} train /{' '}
          {count(fold.n_test)} test
        </p>
        <p className="u-num" style={{ ...T_LABEL, margin: 0 }}>
          PR-AUC {ratio(fold.pr_auc, 3)} · AUROC {ratio(fold.auroc, 3)} · Brier {ratio(fold.brier, 4)}
        </p>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
          {fold.precision_undefined
            ? (fold.precision_note ?? 'precision is undefined for this fold and the run stored no note saying why')
            : `at the budget: precision ${fold.precision_at_budget === null ? 'not stored' : percent(fold.precision_at_budget, 1)}, recall ${
                fold.recall_at_budget === null ? 'not stored' : percent(fold.recall_at_budget, 1)
              }`}{' '}
          · {count(fold.alerts)} alerts · entities disjoint: {fold.entity_disjoint ? 'yes' : 'no'}
        </p>
        <p className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
          captured {moneyText(fold.captured_value)} · cost {moneyText(fold.cost)} · net {moneyText(fold.net_benefit)} ·
          VaR95 {moneyText(fold.var95)} · ES97.5 {moneyText(fold.es975)}
        </p>
        {fold.zero_drawdown_note !== null ? (
          <p style={{ ...T_MICRO, color: 'var(--color-state-done)', margin: 0 }}>{fold.zero_drawdown_note}</p>
        ) : null}
        {fold.test_fold_touched_at !== null ? (
          <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
            test fold touched once, at <Timestamp iso={fold.test_fold_touched_at} timeZone={zone} />
          </p>
        ) : (
          <p style={{ ...T_MICRO, color: 'var(--color-state-running)', margin: 0 }}>
            test fold not touched — the headline for this fold rests on that
          </p>
        )}
      </div>
    </div>
  );
}

/** Money is never rendered without its currency code, and the code comes off the served
 *  Money triple rather than from a constant in this file. */
function moneyText(value: Money): string {
  return `${compactFromMinor(value.minor, value.decimals)} ${value.currency}`;
}

function MetricTable({ metrics, corpora }: { metrics: ValidationMetric[]; corpora: string[] }): ReactElement {
  return (
    <>
      <div className="u-scroll" style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--text-label)', minWidth: 700 }}>
          <thead>
            <tr>
              {['corpus', 'metric', 'value', 'unit', 'n', 'note'].map((heading) => (
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
            {metrics.map((row) => (
              <tr key={`${row.corpus}-${row.name}`} data-metric={row.name}>
                <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, ...T_MONO, fontSize: 'var(--text-micro)' }}>
                  {row.corpus}
                </td>
                <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink)' }}>{row.name}</td>
                <td className="u-num" style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM }}>
                  {ratio(row.value, 4)}
                </td>
                <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}>
                  {row.unit ?? 'no unit stored'}
                </td>
                <td
                  className="u-num"
                  style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-muted)' }}
                >
                  {row.n === null ? '—' : count(row.n)}
                </td>
                <td style={{ padding: '4px 6px', ...HAIRLINE_BOTTOM, color: 'var(--color-ink-faint)' }}>
                  {row.note ?? ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {metrics.length === 0 ? (
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', marginTop: 8 }}>
          The run stored no validation metrics. The corpora named in the bundle are{' '}
          {corpora.length === 0 ? 'none' : corpora.join(', ')}, and there is nothing beside them to report.
        </p>
      ) : (
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
          every figure above is a row the run wrote, keyed on (name, corpus) and never averaged across corpora into one
          headline: PaySim and IBM-AML label different behaviours, and a mean across the two would be a number about
          nothing.
        </p>
      )}
    </>
  );
}

/** One served `CurveSeries`, drawn with the categorical primitives.
 *
 *  The app's line primitive is a TIME axis (`LineChart` parses `x` as an instant), and a
 *  recall axis or a feature axis is not a time axis: the version of this pane that fed
 *  the PR curve through it invented a 1970 date per point to make the numbers fit. Both
 *  curve panes now use `BarWithLine`, whose category label is the served x value and
 *  whose bars are the served y values. Nothing on the axis is composed here. */
function CurvePane({ family, curves }: { family: string; curves: CurveSeries[] }): ReactElement {
  const series = curves.find((entry) => entry.family === family) ?? null;
  if (series === null) {
    return (
      <ChartFrame
        height={220}
        note={`This run stored no “${family}” curve points, so there is no series to draw. The route serves a series only where the pipeline landed the points; an empty chart would read as a result.`}
        label={`${family} absent`}
      />
    );
  }
  const rows = series.points.map((point) => ({
    label: point.label ?? `${series.x_label} ${ratio(point.x, 3)}`,
    value: point.x,
    secondary: point.y,
    colour: 'var(--color-band-b)',
  }));
  const operating = series.points.find((point) => point.operating_point) ?? null;
  const guard = minimumSeriesNote(rows.length, 'bar');
  return (
    <div>
      {guard !== null ? (
        <ChartFrame height={220} note={series.note ?? guard} label={`${family} guard`} />
      ) : (
        <BarWithLine
          rows={rows}
          valueLabel={series.x_label}
          secondaryLabel={series.y_label}
          formatValue={(value) => value.toFixed(3)}
          ariaLabel={`${series.x_label} against ${series.y_label}, per stored point`}
        />
      )}
      <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: '4px 0 0' }}>
        {count(series.points.length)} points · x {series.x_label} · y {series.y_label}
        {series.currency === null ? '' : ` · ${series.currency}`}
        {series.operating_threshold === null
          ? ' · no operating point marked on this series'
          : ` · operating threshold ${ratio(series.operating_threshold, 3)}`}
      </p>
      {operating !== null ? (
        <p style={{ ...T_MICRO, color: 'var(--color-ink)', margin: '2px 0 0' }}>
          chosen operating point: {series.x_label} {ratio(operating.x, 3)} at {series.y_label} {ratio(operating.y, 3)}
          {operating.n === null ? '' : ` · n=${count(operating.n)}`}
        </p>
      ) : null}
      {series.note !== null ? (
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: '4px 0 0' }}>{series.note}</p>
      ) : null}
    </div>
  );
}

function Confusion({ data }: { data: Validation }): ReactElement {
  const confusion = data.confusion;
  if (confusion === null) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '64ch' }}>
        This run stored no confusion cells, so no matrix is drawn. The route answers null rather than a matrix of zeros,
        and the pane keeps the null: a matrix of zeros would claim the model was perfectly wrong in a particular,
        measurable way.
      </p>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {confusion.cells.map((cell) => (
          <div
            key={`${cell.label}-${cell.prediction}`}
            data-confusion-cell={`${cell.label}/${cell.prediction}`}
            style={{ ...PANEL_SUNKEN, padding: 10 }}
          >
            <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: 0 }}>
              {cell.label} · predicted {cell.prediction}
            </p>
            <p className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
              {count(cell.n)}
            </p>
          </div>
        ))}
      </div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        {confusion.budget === null
          ? 'at no named budget: the run recorded no review-budget metric, and a matrix drawn at an unnamed budget is not a matrix at a budget — 0 would claim the desk reviewed nothing.'
          : `at a budget of ${count(confusion.budget)} reviews. `}
        {confusion.basis}
      </p>
    </div>
  );
}

function TypologyRecall({ metrics }: { metrics: ValidationMetric[] }): ReactElement {
  if (metrics.length === 0) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '64ch' }}>
        No typology-recall metric was landed for this run. The pipeline refuses to write one when the artifact records
        no typology with a recall figure, so this is an absence of measurement rather than a recall of zero — and a zero
        here would be a claim about the model that nothing made.
      </p>
    );
  }
  return (
    <Fragment>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {metrics.map((entry) => (
          <li
            key={`${entry.corpus}-${entry.name}`}
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
              {entry.name} <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>{entry.corpus}</span>
            </span>
            <span className="u-num" style={{ ...T_LABEL }}>
              {percent(entry.value, 1)}
            </span>
            <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              {entry.n === null ? 'support not stored' : `n=${count(entry.n)}`}
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

/** The multiple-testing headline plan §12 requires beside the ablation table. Both the
 *  count and the caveat are served in `overfitting`; the count is null when the run
 *  stored no such metric, and the sentence says that instead of printing 0. */
function OverfittingLine({ data }: { data: Validation }): ReactElement {
  const tried = data.overfitting.configurations_evaluated;
  return (
    <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8, maxWidth: '92ch' }}>
      {tried === null
        ? 'No configurations-evaluated count was stored for this run, so the page cannot say how many tries produced the best validation figure.'
        : `${count(tried)} configurations were evaluated during selection, so the best validation result is optimistically biased.`}{' '}
      {data.overfitting.caveat}
    </p>
  );
}

/** Tail behaviour and the Monte Carlo settings, both served per fold.
 *
 *  There is no run-level drawdown, risk-adjusted ratio or seed spread on the wire:
 *  `max_drawdown` and its zero-note are fold fields, the ratio is a stored metric whose
 *  own note carries the label and the formula, and what the run publishes per fold is the
 *  Monte Carlo run count and seed — not a mean and standard deviation across seeds. The
 *  pane reports those and names the missing spread rather than computing one it has no
 *  samples for. */
function Tail({ data, assumptions }: { data: Validation; assumptions: readonly AssumptionLine[] }): ReactElement {
  const riskRatio = data.metrics.find((entry) => entry.name === 'risk_adjusted_benefit_ratio') ?? null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {data.folds.map((fold) => (
        <div key={`${fold.corpus}-${String(fold.fold_index)}`} style={{ ...PANEL_SUNKEN, padding: 10 }}>
          <p style={{ ...T_LABEL, margin: 0 }}>
            fold f{String(fold.fold_index)} · {fold.corpus} · max drawdown
          </p>
          <p className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
            {moneyText(fold.max_drawdown)}
          </p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
            VaR 95% {moneyText(fold.var95)} · ES 97.5% {moneyText(fold.es975)} · Monte Carlo{' '}
            {count(fold.monte_carlo_runs)} runs at seed {String(fold.monte_carlo_seed)}
          </p>
          {fold.zero_drawdown_note !== null ? (
            <p style={{ ...T_MICRO, color: 'var(--color-state-done)', margin: '4px 0 0' }}>{fold.zero_drawdown_note}</p>
          ) : null}
        </div>
      ))}
      {data.folds.length === 0 ? (
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
          No folds, so no drawdown series. The route refuses a validation bundle for a run that wrote none, which is why
          this branch is a guard rather than a state.
        </p>
      ) : null}
      <div style={{ ...PANEL_SUNKEN, padding: 10 }} data-risk-adjusted>
        <p style={{ ...T_LABEL, margin: 0 }}>Risk-adjusted benefit ratio</p>
        {riskRatio === null ? (
          <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: '4px 0 0' }}>
            This run stored no risk_adjusted_benefit_ratio metric, so the ratio is not shown. The page will not restate
            a formula over a number nobody measured.
          </p>
        ) : (
          <>
            <p className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
              {ratio(riskRatio.value, 2)}
              {riskRatio.unit === null ? '' : ` ${riskRatio.unit}`}
            </p>
            {/* The label, the formula and the non-Sharpe statement travel on the metric's
                own note, copied from the stored row (routers/validation.py, and the
                pipeline's `is_sharpe_ratio` field). Printed verbatim. */}
            <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', margin: 0 }}>
              {riskRatio.note ?? 'no note stored with the metric'}
            </p>
          </>
        )}
      </div>
      <Assumptions assumptions={assumptions} />
    </div>
  );
}

/** Degradation, from the envelope — which is where the API puts it.
 *
 *  `meta.degraded` with `meta.degraded_reason` is the served statement; the per-dependency
 *  fallback list this pane used to render was a client shape with no route behind it. */
function Degradation({ meta }: { meta: import('@/lib/api/contract').ListMeta | null }): ReactElement {
  if (meta === null) {
    return (
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
        No provenance block arrived with this response, so the page cannot say whether the run degraded. That is a gap
        in the response, not a clean run.
      </p>
    );
  }
  return (
    <>
      {meta.degraded ? (
        <p style={{ ...T_LABEL, color: 'var(--color-band-d)' }}>
          This run degraded: {meta.degraded_reason ?? 'the response names no reason'}. The pane above it renders the
          deterministic path rather than failing.
        </p>
      ) : (
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
          The envelope reports this run as not degraded. When a dependency does fail, `meta.degraded` and its reason
          appear here and the affected pane keeps working on the deterministic path.
        </p>
      )}
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
        graceful degradation under partial failure is a design requirement here, not an accident of the demo: a dead
        solver or a missing tracking store changes what is claimed, never whether the screen works.
      </p>
    </>
  );
}
