/* =============================================================================
   OXBOW ErrorPane.
   Spec 12.6 state craft, plus the constitution's auditability rule.

   PANE-LEVEL, NOT PAGE-LEVEL. A failing pane retries in place while every other
   pane keeps working. The dashboard has six panes and one of them hitting a
   timeout is not a reason to blank the other five: an analyst mid-investigation
   loses the context they were holding, and the run they were reading is the run
   that produced the evidence they were about to cite.

   EVERY ERROR SURFACE PRINTS THE run_id WITH A COPY BUTTON. An investigation tool
   whose failures cannot be reported is not an investigation tool. The run id is
   the join key between what the analyst saw and what the pipeline logged, so it
   is rendered in IBM Plex Mono, selectable, and one click from the clipboard.

   The error copy says what failed, what still works, and what to do next. It does
   not apologise, and it never shows a raw stack trace to a user.
   ============================================================================= */

import type { CSSProperties, ReactElement, ReactNode } from 'react';

import { Icon } from '../icons/Icon';
import { useCopyButton } from './EmptyState';

/** An RFC 9457 problem+json, narrowed to the fields the UI actually shows. */
export interface ProblemDetail {
  type?: string;
  title: string;
  status?: number;
  detail?: string;
  instance?: string;
  run_id?: string;
  trace_id?: string;
}

export type ErrorTier = 'pane' | 'blocking';

export interface ErrorPaneProps {
  /** What the pane was doing, in the user's words: "Loading the network graph". */
  operation: string;
  /** The failure. Accepts a parsed problem+json or a plain Error. */
  error: ProblemDetail | Error;
  /**
   * Identifies the pane. A dashboard with several panes needs to know which one
   * to retry, and the retry button must act on this pane alone.
   */
  paneId: string;
  /** Retries in place. Must be a single pane-scoped request, never a full reload. */
  onRetry: () => void;
  /** Attempt count, shown so a repeated failure is visibly a repeated failure. */
  attempt?: number;
  /** True while the retry is in flight. Disables the button; no spinner, the
   *  button label carries the state. */
  retrying?: boolean;
  /** What survives this failure, so the user knows what they can still do. */
  siblingsIntact?: boolean;
  /** Extra context rendered under the message: a link, a list, a code block. */
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

function toProblem(error: ProblemDetail | Error): ProblemDetail {
  return error instanceof Error ? { title: error.name || 'Request failed' } : error;
}

export function ErrorPane({
  operation,
  error,
  paneId,
  onRetry,
  attempt = 1,
  retrying = false,
  siblingsIntact = true,
  children,
  className,
  style,
}: ErrorPaneProps): ReactElement {
  const problem = toProblem(error);
  const [copied, copy] = useCopyButton();

  /* A pane that has never produced a run id is a client-side failure, and
   * inventing a fake one would be worse than printing none. Say so instead. */
  const runId = problem.run_id ?? null;

  return (
    <section
      className={className}
      role="alert"
      aria-live="assertive"
      data-pane={paneId}
      style={{
        padding: '12px 14px',
        border: '1px solid var(--color-hairline)',
        /* Left rule, not a border on all four sides and never a shadow: a pane
         * failure is a margin note on the working surface, not a modal. */
        borderLeft: '2px solid var(--color-state-failed)',
        borderRadius: 'var(--radius-panel)',
        background: 'var(--color-canvas-raised)',
        ...style,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <span style={{ color: 'var(--color-state-failed)', marginTop: 1 }}>
          <Icon name="hash-link" size={16} title="Error" />
        </span>

        <div style={{ flex: 1, minWidth: 0 }}>
          <p
            style={{
              margin: 0,
              fontFamily: 'var(--font-sans)',
              fontSize: '0.875rem',
              fontWeight: 600,
              color: 'var(--color-ink)',
            }}
          >
            {operation} failed
          </p>

          <p
            style={{
              margin: '4px 0 0',
              fontFamily: 'var(--font-sans)',
              fontSize: '0.75rem',
              lineHeight: 1.5,
              color: 'var(--color-ink-muted)',
            }}
          >
            {problem.title}
            {typeof problem.status === 'number' ? ` (HTTP ${problem.status})` : ''}
          </p>

          {problem.detail ? (
            <p
              style={{
                margin: '4px 0 0',
                fontFamily: 'var(--font-sans)',
                fontSize: '0.75rem',
                lineHeight: 1.5,
                color: 'var(--color-ink-faint)',
              }}
            >
              {problem.detail}
            </p>
          ) : null}

          {children ? <div style={{ marginTop: 10 }}>{children}</div> : null}

          {/* The run id, always, whenever there is one. */}
          <div
            style={{
              marginTop: 10,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexWrap: 'wrap',
            }}
          >
            {runId !== null ? (
              <>
                <span
                  style={{
                    fontFamily: 'var(--font-sans)',
                    fontSize: '0.6875rem',
                    color: 'var(--color-ink-faint)',
                  }}
                >
                  run_id
                </span>
                <code
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: '0.6875rem',
                    color: 'var(--color-ink)',
                    userSelect: 'all',
                    padding: '1px 4px',
                    border: '1px solid var(--color-hairline)',
                    borderRadius: 'var(--radius-control)',
                  }}
                >
                  {runId}
                </code>
                <button
                  type="button"
                  onClick={() => copy(runId)}
                  style={{
                    fontFamily: 'var(--font-sans)',
                    fontSize: '0.6875rem',
                    fontWeight: 500,
                    color: 'var(--color-ink-muted)',
                    background: 'transparent',
                    border: '1px solid var(--color-hairline-strong)',
                    borderRadius: 'var(--radius-control)',
                    padding: '1px 8px',
                    cursor: 'pointer',
                  }}
                >
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </>
            ) : (
              <span
                style={{
                  fontFamily: 'var(--font-sans)',
                  fontSize: '0.6875rem',
                  color: 'var(--color-ink-faint)',
                }}
              >
                No run_id: this failed before the request reached the pipeline. Retry will start one.
              </span>
            )}
          </div>

          <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={onRetry}
              disabled={retrying}
              style={{
                fontFamily: 'var(--font-sans)',
                fontSize: '0.75rem',
                fontWeight: 500,
                color: 'var(--color-ink-inverse)',
                background: 'var(--color-ink)',
                border: '1px solid var(--color-ink)',
                borderRadius: 'var(--radius-control)',
                padding: '4px 10px',
                cursor: retrying ? 'progress' : 'pointer',
                opacity: retrying ? 0.7 : 1,
                transition: 'opacity var(--duration-instant) var(--ease-out-quint)',
              }}
            >
              {retrying ? 'Retrying…' : 'Retry this pane'}
            </button>

            {attempt > 1 ? (
              <span
                className="u-tabular"
                style={{
                  fontFamily: 'var(--font-sans)',
                  fontSize: '0.6875rem',
                  color: 'var(--color-ink-faint)',
                }}
              >
                attempt {attempt}
              </span>
            ) : null}

            {siblingsIntact ? (
              <span
                style={{
                  fontFamily: 'var(--font-sans)',
                  fontSize: '0.6875rem',
                  color: 'var(--color-ink-faint)',
                }}
              >
                The rest of the page is still live.
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}

export default ErrorPane;
