/* =============================================================================
   Pane — the frame every screen section is built from.

   A pane owns three things the plan requires and a plain <section> does not:

   1. Its OWN error boundary. `react-error-boundary` per pane means one failing
      region renders ErrorPane while every sibling keeps working. The alternative —
      a route-level boundary — is the tier-3 behaviour applied to a tier-2 failure,
      and it costs the analyst the context they were holding.
   2. Its OWN suspense geometry. `suspend` renders a matched-geometry skeleton whose
      row heights and column widths come from the same constants the resolved content
      uses, which is the only reason CLS can be zero rather than small.
   3. Its OWN degraded slot. When a dependency is unavailable the API says so in
      `meta.degraded`, and the pane renders the fallback with a banner rather than
      failing — degraded, not broken.

   The `data-pane` attribute is what the Playwright isolation test selects on: it
   fails one pane and asserts the others still have content.
   ============================================================================= */

'use client';

import { ErrorBoundary } from 'react-error-boundary';
import type { ReactElement, ReactNode } from 'react';

import { ErrorPane } from '../design/primitives/ErrorPane';
import { Skeleton, type SkeletonColumn } from '../design/primitives/Skeleton';
import type { ApiFailure } from '../lib/api/problem';
import { failureDetail, failureStatus, failureTitle, toApiFailure } from '../lib/api/problem';
import type { ListMeta } from '../lib/api/contract';
import { DegradedBanner } from './ui/provenance';
import { HAIRLINE_BOTTOM, PANEL, T_LABEL, T_MICRO } from './ui/sx';

export type PaneProps = {
  id: string;
  title: string;
  /** What the pane was doing, in the user's words, for the error surface. */
  operation: string;
  children: ReactNode;
  meta?: ListMeta | null;
  /** Right-aligned actions: a filter chip, a link, a retry. */
  actions?: ReactNode;
  /** Matched-geometry skeleton spec. Required: a pane with no skeleton is a pane
   *  that shifts when it resolves. */
  skeleton?: { columns: readonly SkeletonColumn[]; rows: number; rowHeight?: number };
  /**
   * Reserved body height in px, applied in BOTH the pending and the resolved state.
   *
   * A skeleton can only promise the geometry the content will actually take, and for a
   * pane whose height depends on how much data arrived (a chart at its real height, a
   * feed plus its assumption line) that promise is a guess. Reserving the box on the
   * container makes the promise structural: the region is that tall whether it is
   * holding a skeleton or the answer, so resolution cannot move anything below it.
   * This is what plan §16's "reserved geometry" clause means in pixels.
   */
  reserveHeight?: number;
  /** The narrowest width at which this pane keeps its column layout. */
  minChildWidth?: number;
  padded?: boolean;
  /**
   * The pane's own typed failure. A failing primary payload is an error surface, not
   * an eternal skeleton: when this is set and nothing resolved, the pane renders the
   * ErrorPane in its reserved geometry with the run id and a retry, exactly like the
   * react-error-boundary path for a raised render.
   */
  failure?: ApiFailure | null;
  /** Retry scoped to this pane's query. The button is disabled while one is in flight. */
  onRetry?: (() => void) | null;
  /** Attempt count carried to the error surface so a repeat failure reads as one. */
  attempt?: number;
  retrying?: boolean;
};

export function Pane({
  id,
  title,
  operation,
  children,
  meta = null,
  actions = null,
  skeleton,
  reserveHeight,
  padded = true,
  failure = null,
  onRetry = null,
  attempt = 1,
  retrying = false,
}: PaneProps): ReactElement {
  return (
    <section
      data-pane={id}
      style={{
        ...PANEL,
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        overflow: 'hidden',
      }}
    >
      <header
        data-print-hide
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          ...HAIRLINE_BOTTOM,
          flexWrap: 'wrap',
        }}
      >
        <h2 style={{ ...T_LABEL, margin: 0, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--color-ink)' }}>
          {title}
        </h2>
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>{actions}</span>
      </header>

      <div style={{ flex: 1, minWidth: 0, padding: padded ? '12px' : 0, ...(reserveHeight === undefined ? {} : { minHeight: reserveHeight }) }}>
        <ErrorBoundary
          fallbackRender={({ error, resetErrorBoundary }) => (
            <PaneFailure id={id} operation={operation} error={error} onRetry={resetErrorBoundary} />
          )}
        >
          {meta?.degraded === true ? (
            <DegradedBanner dependency={title} fallback={meta.degraded_reason ?? 'the deterministic path'} meta={meta} />
          ) : null}
          <MaybeSuspense
            skeleton={skeleton}
            label={title}
            meta={meta}
            failure={failure}
            onRetry={onRetry}
            attempt={attempt}
            retrying={retrying}
            paneId={id}
            operation={operation}
          >
            {children}
          </MaybeSuspense>
        </ErrorBoundary>
      </div>
    </section>
  );
}

/** Renders the matched-geometry skeleton while the pane's own query is pending, and
 *  the pane's ErrorPane when the query failed — the reserved geometry is identical
 *  either way, which is what keeps resolution (and failure) at zero layout shift. */
function MaybeSuspense({
  skeleton,
  label,
  meta,
  failure,
  onRetry,
  attempt,
  retrying,
  paneId,
  operation,
  children,
}: {
  skeleton: PaneProps['skeleton'];
  label: string;
  meta: ListMeta | null;
  failure: ApiFailure | null;
  onRetry: (() => void) | null;
  attempt: number;
  retrying: boolean;
  paneId: string;
  operation: string;
  children: ReactNode;
}): ReactElement {
  if (failure !== null && meta === null) {
    return (
      <QueryFailure
        id={paneId}
        operation={operation}
        failure={failure}
        onRetry={onRetry ?? (() => undefined)}
        attempt={attempt}
        retrying={retrying}
      />
    );
  }
  if (skeleton === undefined || skeleton.columns.length === 0 || meta !== null) return <>{children}</>;
  return (
    <Skeleton
      columns={skeleton.columns}
      rows={skeleton.rows}
      rowHeight={skeleton.rowHeight}
      label={`${label} is loading`}
    />
  );
}

/** The pane-level surface for a failed query (as opposed to a raised render). */
function QueryFailure({
  id,
  operation,
  failure,
  onRetry,
  attempt,
  retrying,
}: {
  id: string;
  operation: string;
  failure: ApiFailure;
  onRetry: () => void;
  attempt: number;
  retrying: boolean;
}): ReactElement {
  return (
    <ErrorPane
      paneId={id}
      operation={operation}
      error={failureToProblem(failure)}
      onRetry={onRetry}
      attempt={attempt}
      retrying={retrying}
      siblingsIntact
    >
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
        This pane failed on its own. Every other pane on the page resolved and is still interactive.
      </p>
    </ErrorPane>
  );
}

function PaneFailure({
  id,
  operation,
  error,
  onRetry,
}: {
  id: string;
  operation: string;
  error: unknown;
  onRetry: () => void;
}): ReactElement {
  const failure: ApiFailure | null = toApiFailure(error);
  const problem = failureToProblem(failure);
  return (
    <ErrorPane
      paneId={id}
      operation={operation}
      error={problem}
      onRetry={onRetry}
      siblingsIntact
    >
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>
        This pane failed on its own. Every other pane on the page resolved and is still interactive.
      </p>
    </ErrorPane>
  );
}

/** Adapts the typed failure to ErrorPane's `ProblemDetail | Error` input.
 *  A thrown non-API error keeps its own message rather than being dressed up. */
function failureToProblem(failure: ApiFailure | null): Parameters<typeof ErrorPane>[0]['error'] {
  if (failure === null) return new Error('the pane raised while rendering');
  return {
    type: failure.kind === 'problem' ? failure.problem.type : failure.kind,
    title: failureTitle(failure),
    status: failureStatus(failure) ?? undefined,
    detail: failureDetail(failure) ?? undefined,
    run_id: failure.kind === 'problem' ? (failure.problem.run_id ?? undefined) : undefined,
    trace_id: failure.kind === 'problem' ? (failure.problem.trace_id ?? undefined) : undefined,
  };
}

export { failureToProblem };
