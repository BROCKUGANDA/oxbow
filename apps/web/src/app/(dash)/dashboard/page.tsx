/* =============================================================================
   Command dashboard — plan §14 P8a-1.

   The top strip is in currency, not counts, and in that order: expected loss
   avoided with its r band, benefit per analyst-hour, residual exposure at ES97.5.
   A count first would be the ordinary dashboard; the argument this product makes is
   that monitoring has a price, so the price is what is largest on the page.

   Second row is model quality with its delta against the named baseline, because a
   PR-AUC without the thing it beat is a number with no comparison. Then the
   cumulative benefit curve (never-cut list), the band distribution, and the
   latest-pattern feed.

   Every figure is a `MoneyFigure` component, which cannot render without its
   assumption line, so the plan §11 clause is structural here rather than a caption
   someone remembered to add.
   ============================================================================= */

'use client';

import Link from 'next/link';
import type { CSSProperties, ReactElement } from 'react';

import { Pane } from '@/components/Pane';
import { MultiLineChart } from '@/components/charts/charts';
import { TYPOLOGY_META, glyphFor } from '@/components/typology';
import { BandBadge } from '@/components/ui/BandBadge';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { Assumptions, Timestamp } from '@/components/ui/provenance';
import { GAP_TIGHT, PANEL_SUNKEN, T_LABEL, T_MICRO } from '@/components/ui/sx';
import { Icon } from '@/design/icons/Icon';
import { EmptyState } from '@/design/primitives/EmptyState';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { Shimmer } from '@/design/primitives/Shimmer';
import { ROUTES } from '@/lib/api/contract';
import { useResource } from '@/lib/api/hooks';
import { failureDetail, failureRunId, failureTitle, isRunNotFound } from '@/lib/api/problem';
import { PIPELINE_COMMAND, RUNTIME_ESTIMATE_FALLBACK } from '@/lib/copy';
import { compactFromMinor, count, moneyAxisFormatter } from '@/lib/format/money';

/** The strip's geometry, measured on the resolved page at the desktop breakpoint:
 *  five cells in one `auto-fit minmax(220px,1fr)` row, 123px tall. The pending strip
 *  reserves exactly this box — the same grid template, the same row height — because a
 *  KPI strip that appears 123px taller than its own skeleton moves everything below it,
 *  which is the 0.39 CLS this page measured before the geometry was matched. */
const STRIP_CELLS = ['loss', 'hour', 'residual', 'alerts', 'networks'] as const;
const STRIP_ROW_HEIGHT = 123;
/** The model-quality row is 75px tall resolved, and the chips are static labels, so the
 *  skeleton is the same three boxes at the same height. */
const CHIP_ROW_HEIGHT = 75;
/* Reserved pane bodies, measured on the resolved page at 1440px: the curve pane is
 * 316px of chart plus its assumption line, the feed is six 32px rows plus its own.
 * `Pane` applies these in both states, which is why the numbers can be approximate
 * without costing anything — the box is the box either way. */
const CURVE_BODY = 316;
const PATTERNS_BODY = 262;

/* The three containers below are declared once and used by the pending tree and the
 * resolved tree alike. That is the point: a skeleton cannot match geometry it does not
 * share, and two copies of a grid template is how a page starts disagreeing with itself. */
const PAGE: CSSProperties = {
  padding: 'var(--spacing-pane-gap)',
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--spacing-pane-gap)',
};
const STRIP_GRID: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
  gap: 'var(--spacing-pane-gap)',
};
const FIGURE_GRID: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)',
  gap: 'var(--spacing-pane-gap)',
};

/* ONE source for the pane skeletons, used by both the pending and the resolved tree.
 * The heights below are the measured content heights of the panes they stand in for. */
const PANE_CURVE = { columns: [{ key: 'curve', width: '100%' }], rows: 1, rowHeight: 315 };
const PANE_BANDS = {
  columns: [
    { key: 'band', width: '40%' },
    { key: 'share', width: '60%' },
  ],
  rows: 5,
  rowHeight: 32,
};
const PANE_PATTERNS = {
  columns: [
    { key: 'rule', width: '44px' },
    { key: 'account', width: '140px' },
    { key: 'observed', width: 'minmax(0, 1fr)' },
    { key: 'exposure', width: '140px', align: 'end' as const },
    { key: 'seen', width: '190px', align: 'end' as const },
  ],
  rows: 6,
  rowHeight: 32,
};

export default function DashboardPage(): ReactElement {
  const dashboard = useResource('dashboard', ROUTES.dashboard.path, ROUTES.dashboard.data);
  const runtime = useResource('runtime', ROUTES.runtime.path, ROUTES.runtime.data);
  const timeZone = runtime.data?.deployment_timezone ?? 'UTC';
  const assumptions = dashboard.meta?.assumptions ?? [];

  /* The pending tree is the resolved tree with its data cells replaced by matched-geometry
   * shimmers: same wrapper, same gaps, same pane specs, so nothing below moves on resolve. */
  if (dashboard.data === null) {
    /* A fresh install is an EMPTY state, not an eternal skeleton and not a retry
       invitation: P7 answers run-not-found 404 until the first pipeline completes, and
       plan §14 requires the no-run screen — the literal command, copy button, expected
       runtime — over the reserved geometry. */
    if (dashboard.failure !== null && isRunNotFound(dashboard.failure)) {
      return (
        /* The reserved box below is the height of the pending tree this state
           replaces (strip + chips + two panes + feed + gaps, measured 957 px at
           1440×900). Emptying a smaller box is exactly the layout shift the
           zero-CLS clause is about: the swap must not resize the page. */
        <div data-cls-anchor="dashboard-empty" style={{ ...PAGE, minHeight: 957 }}>
          <EmptyState
            kind="no-run"
            command={PIPELINE_COMMAND}
            expectedRuntime={RUNTIME_ESTIMATE_FALLBACK}
            corpus={runtime.data?.dataset ?? undefined}
          />
        </div>
      );
    }
    return (
      <div style={PAGE}>
        {/* A failed primary payload is an error, not an eternal skeleton: the strip says
            what broke, prints the run_id, and retries itself while the rest of the page
            keeps whatever the other routes returned. */}
        {dashboard.failure !== null ? (
          <ErrorPane
            paneId="dashboard-strip"
            operation="Loading the command strip"
            error={{
              title: failureTitle(dashboard.failure),
              detail: failureDetail(dashboard.failure) ?? undefined,
              run_id: failureRunId(dashboard.failure) ?? undefined,
            }}
            onRetry={() => void dashboard.refetch()}
            attempt={dashboard.attempts}
            retrying={dashboard.isFetching}
          />
        ) : null}
        <section
          aria-hidden="true"
          aria-label="Period economics, loading"
          style={{ ...STRIP_GRID, minHeight: STRIP_ROW_HEIGHT }}
        >
          {STRIP_CELLS.map((key) => (
            <Shimmer key={key} width="100%" height={STRIP_ROW_HEIGHT} radius="var(--radius-panel)" />
          ))}
        </section>

        <section
          aria-hidden="true"
          aria-label="Model quality, loading"
          style={{ display: 'flex', gap: 8, flexWrap: 'wrap', minHeight: CHIP_ROW_HEIGHT }}
        >
          {['PR-AUC', 'Precision at budget', 'Brier'].map((label) => (
            <Shimmer key={label} width={220} height={CHIP_ROW_HEIGHT} radius="var(--radius-control)" />
          ))}
        </section>

        <div style={FIGURE_GRID}>
          <Pane
            id="curve"
            title="Cumulative benefit by policy"
            operation="Loading the cumulative benefit curve"
            skeleton={PANE_CURVE}
            reserveHeight={CURVE_BODY}
          >
            <span />
          </Pane>
          <Pane
            id="bands"
            title="Band distribution"
            operation="Loading the band distribution"
            skeleton={PANE_BANDS}
            reserveHeight={CURVE_BODY}
          >
            <span />
          </Pane>
        </div>

        <Pane
          id="patterns"
          title="Latest patterns"
          operation="Loading the pattern feed"
          skeleton={PANE_PATTERNS}
          reserveHeight={PATTERNS_BODY}
        >
          <span />
        </Pane>
      </div>
    );
  }

  const data = dashboard.data;
  const totalAccounts = data.band_distribution.reduce((sum, bucket) => sum + bucket.accounts, 0);
  const noRunYet = data.alerts_generated === 0 && data.latest_patterns.length === 0;

  return (
    <div style={PAGE}>
      {/* ---- the currency strip ---------------------------------------- */}
      <section data-strip aria-label="Period economics" style={STRIP_GRID}>
        <MoneyFigure
          figure={data.expected_loss_avoided}
          assumptions={assumptions}
          label="Expected loss avoided this period"
          emphasis="kpi"
          source={data.economics_source}
        />
        <MoneyFigure
          figure={data.benefit_per_analyst_hour}
          assumptions={assumptions}
          label="Benefit per analyst-hour"
          emphasis="kpi"
          source={data.economics_source}
        />
        <MoneyFigure
          figure={data.residual_exposure}
          assumptions={assumptions}
          label="Residual exposure (ES 97.5%)"
          emphasis="kpi"
          source={data.economics_source}
        />

        {/* Counts are the third row of the strip and smaller: the plan's clause is
            "currency, not counts", so the two counts that still matter come after. */}
        <div style={{ ...PANEL_SUNKEN, padding: 10 }}>
          <p style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Alerts generated</p>
          <p
            className="u-num"
            style={{
              fontFamily: 'var(--font-condensed)',
              fontSize: 'var(--text-kpi)',
              fontWeight: 600,
              margin: '2px 0 0',
            }}
          >
            {count(data.alerts_generated)}
          </p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            {count(data.capacity.reviewed)} reviewed of {count(data.capacity.available)} {data.capacity.unit} ·{' '}
            {data.capacity.period_label}
          </p>
        </div>

        <div style={{ ...PANEL_SUNKEN, padding: 10 }}>
          <p style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em' }}>High-risk networks</p>
          <p
            className="u-num"
            style={{
              fontFamily: 'var(--font-condensed)',
              fontSize: 'var(--text-kpi)',
              fontWeight: 600,
              margin: '2px 0 0',
            }}
          >
            {count(data.high_risk_networks)}
          </p>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            components with a flagged member ·{' '}
            <Link href="/network" style={{ textDecoration: 'underline' }}>
              open the explorer
            </Link>
          </p>
        </div>
      </section>

      {/* ---- model quality chips --------------------------------------- */}
      <section aria-label="Model quality" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <Chip label="PR-AUC" metric={data.model_quality.pr_auc} />
        <Chip label="Precision at budget" metric={data.model_quality.precision_at_budget} />
        <Chip label="Brier" metric={data.model_quality.brier} lowerIsBetter />
      </section>

      {noRunYet ? (
        <EmptyState
          kind="no-run"
          command={PIPELINE_COMMAND}
          expectedRuntime={RUNTIME_ESTIMATE_FALLBACK}
          corpus={runtime.data?.dataset ?? undefined}
        />
      ) : null}

      {/* ---- the curve, the distribution, the feed ---------------------- */}
      <div style={FIGURE_GRID}>
        <Pane
          id="curve"
          title="Cumulative benefit by policy"
          operation="Loading the cumulative benefit curve"
          meta={dashboard.meta}
          skeleton={PANE_CURVE}
          reserveHeight={CURVE_BODY}
        >
          <MultiLineChart
            series={data.cumulative_benefit.series}
            yIsMoney={data.cumulative_benefit.y_is_money}
            formatY={moneyAxisFormatter(data.expected_loss_avoided.value.decimals)}
            ariaLabel={`${data.cumulative_benefit.y_axis_label} by ${data.cumulative_benefit.x_axis_label}, one line per policy`}
          />
          <Assumptions assumptions={assumptions} source={data.economics_source} />
        </Pane>

        <Pane
          id="bands"
          title="Band distribution"
          operation="Loading the band distribution"
          meta={dashboard.meta}
          skeleton={PANE_BANDS}
          reserveHeight={CURVE_BODY}
        >
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {data.band_distribution.map((bucket) => (
              <li
                key={bucket.band}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '56px 1fr auto',
                  alignItems: 'center',
                  gap: 8,
                  height: 'var(--spacing-row)',
                  borderBottom: '1px solid var(--color-hairline)',
                }}
              >
                <BandBadge band={bucket.band} describe={false} />
                <span
                  aria-hidden="true"
                  style={{ height: 8, background: 'var(--color-hairline)', position: 'relative' }}
                >
                  <span
                    style={{
                      position: 'absolute',
                      inset: 0,
                      width: `${Math.round(bucket.population_share * 100)}%`,
                      background: `var(--color-band-${bucket.band.toLowerCase()})`,
                    }}
                  />
                </span>
                <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                  {count(bucket.accounts)} · {(bucket.population_share * 100).toFixed(1)}%
                </span>
              </li>
            ))}
          </ul>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 8 }}>
            {count(totalAccounts)} scored accounts. Bands are ordered by observed bad rate on validation, and each
            carries its letter and five-segment meter as well as its colour.
          </p>
        </Pane>
      </div>

      <Pane
        id="patterns"
        title="Latest patterns"
        operation="Loading the pattern feed"
        meta={dashboard.meta}
        skeleton={PANE_PATTERNS}
        reserveHeight={PATTERNS_BODY}
      >
        {data.latest_patterns.length === 0 ? (
          <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '64ch' }}>
            No rule crossed its threshold in {data.capacity.period_label}, although {count(data.alerts_generated)}{' '}
            accounts were scored. A feed that is quiet because nothing fired is a result, and it is worth checking the
            thresholds before reading it as a clean period.
          </p>
        ) : (
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {data.latest_patterns.map((hit) => (
              <li
                key={`${hit.rule_code}-${hit.account_key}`}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '44px 140px minmax(0, 1fr) 160px 200px',
                  alignItems: 'center',
                  gap: 8,
                  minHeight: 'var(--spacing-row)',
                  borderBottom: '1px solid var(--color-hairline)',
                }}
              >
                <span
                  title={`${hit.rule_code} · ${TYPOLOGY_META[hit.typology].reads}`}
                  style={{ color: 'var(--color-ink-muted)' }}
                >
                  <Icon
                    name={glyphFor(hit.typology)}
                    size={16}
                    title={`${hit.rule_code} ${TYPOLOGY_META[hit.typology].name}`}
                  />
                </span>
                <Link
                  href={hit.case_href}
                  style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--text-label)', color: 'var(--color-ink)' }}
                >
                  {hit.account_key}
                </Link>
                <span
                  style={{
                    ...T_LABEL,
                    color: 'var(--color-ink-muted)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={hit.observed}
                >
                  {hit.rule_code} · {hit.observed}
                </span>
                <span className="u-num" style={{ ...T_LABEL, textAlign: 'right', color: 'var(--color-ink)' }}>
                  {compactFromMinor(hit.exposure.value.minor, hit.exposure.value.decimals)}{' '}
                  {hit.exposure.value.currency}
                </span>
                <span style={{ textAlign: 'right' }}>
                  <Timestamp iso={hit.first_seen} timeZone={timeZone} sense="first seen" />
                </span>
              </li>
            ))}
          </ul>
        )}
        {/* The feed's exposure column is money, so the pane names the keys that money
            is a function of — plan §11's clause applies to a figure in a table cell as
            much as to one in a KPI strip. */}
        <Assumptions assumptions={assumptions} source={data.economics_source} />
      </Pane>
    </div>
  );
}

/** A model-quality chip: the value, its delta, and the baseline the delta is against. */
function Chip({
  label,
  metric,
  lowerIsBetter = false,
}: {
  label: string;
  metric: {
    value: number;
    delta_vs_baseline: number | null;
    baseline_label: string | null;
    unit: string;
    ci: number[] | null;
  };
  lowerIsBetter?: boolean;
}): ReactElement {
  const delta = metric.delta_vs_baseline;
  const improving = delta === null ? null : lowerIsBetter ? delta < 0 : delta > 0;
  return (
    <div
      style={{ ...PANEL_SUNKEN, padding: '8px 10px', display: 'flex', flexDirection: 'column', ...GAP_TIGHT }}
      data-chip={label}
    >
      <p style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</p>
      <p style={{ display: 'flex', alignItems: 'baseline', gap: 6, margin: 0 }}>
        <span
          className="u-num"
          style={{
            fontFamily: 'var(--font-condensed)',
            fontSize: 'var(--text-kpi)',
            fontWeight: 600,
            color: 'var(--color-ink)',
          }}
        >
          {metric.value.toFixed(metric.unit === 'Brier' ? 4 : 3)}
        </span>
        {delta !== null ? (
          <span
            className="u-num"
            style={{
              ...T_MICRO,
              color:
                improving === null
                  ? 'var(--color-ink-faint)'
                  : improving
                    ? 'var(--color-state-done)'
                    : 'var(--color-state-failed)',
            }}
          >
            {delta > 0 ? '+' : ''}
            {delta.toFixed(metric.unit === 'Brier' ? 4 : 3)} vs {metric.baseline_label ?? 'baseline'}
          </span>
        ) : (
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>no baseline recorded for this run</span>
        )}
        {metric.ci !== null && metric.ci.length === 2 ? (
          <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            95% CI {metric.ci[0]?.toFixed(3)}–{metric.ci[1]?.toFixed(3)}
          </span>
        ) : null}
      </p>
    </div>
  );
}
