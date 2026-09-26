/* =============================================================================
   OXBOW StageLedger.
   Spec 12.7 MOTION, and the "a progress UI that lies is the failure being
   prevented" clause from the SSE contract.

   A long job gets a LEDGER, not a spinner. A spinner says "something is
   happening somewhere" and is therefore indistinguishable between a run that is
   four seconds from done and a run that has wedged. The ledger says exactly
   which stage is running, how many rows it has produced, and how long it has
   taken, because that is the information an analyst needs to decide whether to
   wait, go and do something else, or kill the run.

   Rows fill with elapsed time and row counts as each stage completes, driven by
   SSE stage events: { id, stage, status, rows, elapsed_ms }. The component is a
   pure function of those events plus a wall clock, so a resumed connection
   replaying from Last-Event-ID produces the same ledger, not a doubled one.

   NO SPINNER FOR CONTENT. A stage that is running shows a filling bar, a live
   elapsed figure, and whatever row count the server has reported so far. If the
   server has not reported rows yet, the ledger says so rather than inventing a
   number that crawls upward on its own.
   ============================================================================= */

import type { CSSProperties, ReactElement } from 'react';
import { useEffect, useState } from 'react';

import { DURATION, usePrefersReducedMotion } from '../motion';
import { useCopyButton } from './EmptyState';

export type StageStatus = 'pending' | 'running' | 'complete' | 'failed' | 'skipped';

/** One SSE stage event. Mirrors the Pydantic model on the server. */
export interface StageEvent {
  id: string;
  stage: string;
  status: StageStatus;
  rows: number | null;
  elapsed_ms: number | null;
}

export interface StageLedgerProps {
  /** Stages in execution order, including the ones not yet started. */
  stages: readonly StageEvent[];
  /** The run this ledger belongs to. Printed on every failure. */
  runId?: string;
  /** Called with the stage id when a failed stage's row is activated. */
  onRetryStage?: (stage: StageEvent) => void;
  /** Tallies the user has seen. Cheap, and it removes the "did it finish?" question. */
  showTotals?: boolean;
  className?: string;
  style?: CSSProperties;
}

const STATUS_LABEL: Readonly<Record<StageStatus, string>> = {
  pending: 'queued',
  running: 'running',
  complete: 'complete',
  failed: 'failed',
  skipped: 'skipped',
};

const STATUS_COLOUR: Readonly<Record<StageStatus, string>> = {
  pending: 'var(--color-state-pending)',
  running: 'var(--color-state-running)',
  complete: 'var(--color-state-done)',
  failed: 'var(--color-state-failed)',
  skipped: 'var(--color-state-skipped)',
};

function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${String(rest).padStart(2, '0')}s`;
}

export function StageLedger({
  stages,
  runId,
  onRetryStage,
  showTotals = true,
  className,
  style,
}: StageLedgerProps): ReactElement {
  const [now, setNow] = useState<number | null>(null);
  const reducedMotion = usePrefersReducedMotion();
  const [copied, copy] = useCopyButton();

  /* A wall clock is only read while something is actually running, so a ledger
   * showing a completed run has no timer repainting behind it. */
  const hasRunning = stages.some((s) => s.status === 'running');

  useEffect(() => {
    if (!hasRunning) {
      setNow(null);
      return;
    }
    setNow(Date.now());
    const handle = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(handle);
  }, [hasRunning]);

  const totals = stages.reduce(
    (acc, stage) => {
      acc.rows += stage.rows ?? 0;
      acc.elapsed += stage.elapsed_ms ?? 0;
      if (stage.status === 'complete') acc.complete += 1;
      return acc;
    },
    { rows: 0, elapsed: 0, complete: 0 },
  );

  const failed = stages.find((s) => s.status === 'failed') ?? null;

  return (
    <section
      className={className}
      aria-label="Pipeline stages"
      style={{
        border: '1px solid var(--color-hairline)',
        borderRadius: 'var(--radius-panel)',
        background: 'var(--color-canvas-raised)',
        overflow: 'hidden',
        ...style,
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          padding: '8px 12px',
          borderBottom: '1px solid var(--color-hairline)',
        }}
      >
        <span
          style={{
            fontFamily: 'var(--font-sans)',
            fontSize: '0.6875rem',
            fontWeight: 500,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
            color: 'var(--color-ink-faint)',
          }}
        >
          Stage ledger
        </span>

        {showTotals ? (
          <span
            className="u-tabular"
            style={{
              fontFamily: 'var(--font-sans)',
              fontSize: '0.6875rem',
              color: 'var(--color-ink-muted)',
            }}
          >
            {totals.complete}/{stages.length} complete · {totals.rows.toLocaleString('en-US')} rows ·{' '}
            {formatMs(totals.elapsed)}
          </span>
        ) : null}
      </header>

      <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {stages.map((stage) => (
          <StageRow
            key={stage.id}
            stage={stage}
            now={now}
            reducedMotion={reducedMotion}
            onRetryStage={onRetryStage}
          />
        ))}
      </ol>

      {/* A failure in the ledger is an error surface, so it prints the run_id too. */}
      {failed !== null ? (
        <footer
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
            padding: '8px 12px',
            borderTop: '1px solid var(--color-hairline)',
            background: 'var(--color-canvas-sunken)',
          }}
        >
          <span style={{ fontSize: '0.6875rem', color: 'var(--color-ink-faint)', fontFamily: 'var(--font-sans)' }}>
            stage <span style={{ color: 'var(--color-ink)' }}>{failed.stage}</span> failed
          </span>
          {runId !== undefined ? (
            <>
              <code
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: '0.6875rem',
                  color: 'var(--color-ink)',
                  userSelect: 'all',
                }}
              >
                {runId}
              </code>
              <LedgerButton onClick={() => copy(runId)}>{copied ? 'Copied' : 'Copy run_id'}</LedgerButton>
            </>
          ) : null}
        </footer>
      ) : null}
    </section>
  );
}

interface StageRowProps {
  stage: StageEvent;
  now: number | null;
  reducedMotion: boolean;
  onRetryStage: ((stage: StageEvent) => void) | undefined;
}

function StageRow({ stage, now, reducedMotion, onRetryStage }: StageRowProps): ReactElement {
  const running = stage.status === 'running';

  /* While a stage runs, elapsed is the live difference between when the event
   * arrived and now. The moment it completes, elapsed_ms from the server wins,
   * because the server's number is the one the log has and the one the run is
   * audited on. Never blend the two. */
  const liveElapsed = running && now !== null && stage.elapsed_ms !== null ? now - stage.elapsed_ms : null;
  const shownElapsed = liveElapsed ?? stage.elapsed_ms;

  /* Fill width: a completed or failed stage is 100% because it is finished, a
   * pending stage is 0%, and a running stage fills over a nominal 12s. The bar
   * is a progress affordance, not a promise: the elapsed figure beside it is the
   * number to trust, and the 12s denominator is a visible constant rather than a
   * moving target. */
  const fill = stage.status === 'complete' || stage.status === 'failed' ? 100 : running ? 45 : 0;

  return (
    <li
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr auto',
        gridTemplateRows: 'auto auto',
        columnGap: 12,
        padding: '10px 12px',
        borderBottom: '1px solid var(--color-hairline)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <span
          aria-hidden="true"
          style={{
            width: 6,
            height: 6,
            flexShrink: 0,
            background: STATUS_COLOUR[stage.status],
          }}
        />
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.75rem',
            color: stage.status === 'pending' ? 'var(--color-ink-faint)' : 'var(--color-ink)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {stage.stage}
        </span>
        <span
          style={{
            fontFamily: 'var(--font-sans)',
            fontSize: '0.6875rem',
            /* The word carries the state; the dot above carries the colour. Inking the
               word with STATUS_COLOUR put `queued` at 2.3:1 and `skipped` at 3.0:1 on
               this panel — text at those ratios is a WCAG 1.4.3 failure, and the state
               was already spelled out, so the colour was never the only channel. */
            color: 'var(--color-ink-muted)',
          }}
        >
          {STATUS_LABEL[stage.status]}
        </span>
      </div>

      <div
        className="u-tabular"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          fontFamily: 'var(--font-sans)',
          fontSize: '0.6875rem',
          color: 'var(--color-ink-muted)',
          whiteSpace: 'nowrap',
        }}
      >
        {stage.rows !== null ? <span>{stage.rows.toLocaleString('en-US')} rows</span> : null}
        {shownElapsed !== null ? <span>{formatMs(shownElapsed)}</span> : null}
        {stage.status === 'failed' && onRetryStage ? (
          <LedgerButton onClick={() => onRetryStage(stage)}>Rerun</LedgerButton>
        ) : null}
      </div>

      {/* The fill. transform-only under reduced motion, 2px tall, no radius:
          this is a rule on a ledger, not a rounded progress capsule.

          Width is animated, not transform, because the fill length IS the datum
          here. It is a width transition on one composited layer inside an
          overflow-hidden box, which is the cheap case; the compositor-only rule
          governs the shimmer, where the sweep covers a region much larger than
          the element animating. */}
      <div
        aria-hidden="true"
        style={{
          gridColumn: '1 / -1',
          marginTop: 6,
          height: 2,
          background: 'var(--color-hairline)',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${fill}%`,
            background: STATUS_COLOUR[stage.status],
            transition: reducedMotion ? 'none' : `${widthTransition(running)}`,
          }}
        />
      </div>
    </li>
  );
}

/** A running bar overshoots slightly on the deliberate spring; a settled one does not. */
function widthTransition(running: boolean): string {
  const duration = `${DURATION.deliberate}ms`;
  const easing = running ? 'cubic-bezier(0.34, 1.2, 0.64, 1)' : 'var(--ease-out-quint)';
  return `width ${duration} ${easing}`;
}

function LedgerButton({ children, onClick }: { children: string; onClick: () => void }): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        fontFamily: 'var(--font-sans)',
        fontSize: '0.6875rem',
        fontWeight: 500,
        color: 'var(--color-ink)',
        background: 'transparent',
        border: '1px solid var(--color-hairline-strong)',
        borderRadius: 'var(--radius-control)',
        padding: '1px 8px',
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  );
}

export default StageLedger;
