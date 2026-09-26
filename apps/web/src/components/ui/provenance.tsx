/* =============================================================================
   Small display primitives that carry a provenance obligation.

   Each of these exists because the alternative is a specific failure:
   * `AccountChip` — raw identifiers must never appear (02 §F PII boundary). The
     chip is the only place an account is named, it is mono because it is an
     evidence string, and it is a link so a deep link from the queue lands somewhere.
   * `Timestamp` — a UTC instant shown to a UTC+3 analyst is a demo-day bug, so the
     zone abbreviation and the offset are rendered as part of the value and the
     underlying instant is in the title for anyone who needs it.
   * `RunIdChip` — every error surface prints the run id with a copy button, and
     this is the only component that prints one, so the rule is enforced in one place.
   * `ProvenanceBadge` / `DegradedBanner` — the difference between a measurement, a
     null-adapter demo and a fixture, and the difference between a failure and a
     fallback, both have to be visible without reading the code.
   ============================================================================= */

import type { ReactElement } from 'react';
import Link from 'next/link';

import { Icon } from '../../design/icons/Icon';
import { CopyButton } from '../../design/primitives/EmptyState';
import type { AssumptionLine, Band, Meta } from '../../lib/api/contract';
import { formatInstant, zoned } from '../../lib/format/time';
import { ELLIPSIS, PANEL_SUNKEN, T_LABEL, T_MICRO, T_MONO } from './sx';

/* ------------------------------------------------------------- account ---- */

export function AccountChip({ accountKey, href }: { accountKey: string; href: string }): ReactElement {
  return (
    <Link
      href={href}
      style={{
        ...T_MONO,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '1px 8px',
        fontSize: 'var(--text-label)',
        color: 'var(--color-ink)',
        background: 'var(--color-elev-1)',
        border: '1px solid var(--color-hairline)',
        borderRadius: 'var(--radius-pill)',
        textDecoration: 'none',
        whiteSpace: 'nowrap',
      }}
    >
      {accountKey}
    </Link>
  );
}

/* ----------------------------------------------------------- timestamp ---- */

export function Timestamp({
  iso,
  timeZone,
  sense,
}: {
  iso: string | null;
  /** Null while `GET /api/meta/run` is unresolved. The component then says the zone
   *  is unreported rather than defaulting to UTC, which is the bug this file exists
   *  to prevent. */
  timeZone: string | null;
  /** Stated so a reader knows whether this is when it happened or when it was scored. */
  sense?: string;
}): ReactElement {
  if (timeZone === null) {
    return (
      <time dateTime={iso ?? undefined} data-timestamp-timezone={null} style={{ ...T_LABEL, color: 'var(--color-ink-faint)' }}>
        {iso ?? 'no instant recorded'} · zone unreported
      </time>
    );
  }
  const instant = zoned(iso, timeZone);
  return (
    <time
      dateTime={iso ?? undefined}
      data-timestamp-timezone={instant.missing ? undefined : timeZone}
      title={iso ?? 'no instant recorded'}
      style={{ ...T_LABEL, color: 'var(--color-ink-muted)', whiteSpace: 'nowrap' }}
    >
      {sense ? <span className="u-sr-only">{sense}: </span> : null}
      {formatInstant(iso, timeZone)}
    </time>
  );
}

/* ------------------------------------------------------------- run id ----- */

export function RunIdChip({ runId, traceId = null }: { runId: string | null; traceId?: string | null }): ReactElement {
  if (runId === null) {
    return (
      <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        No run_id: this failed before the request reached the pipeline.
      </span>
    );
  }
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>run_id</span>
      <code
        data-run-id={runId}
        style={{
          ...T_MONO,
          fontSize: 'var(--text-micro)',
          padding: '1px 4px',
          border: '1px solid var(--color-hairline)',
          borderRadius: 'var(--radius-control)',
          color: 'var(--color-ink)',
          userSelect: 'all',
        }}
      >
        {runId}
      </code>
      <CopyButton text={runId} label="Copy" />
      {traceId !== null ? (
        <>
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>trace_id</span>
          <code style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink-muted)' }}>{traceId}</code>
        </>
      ) : null}
    </span>
  );
}

/* ----------------------------------------------------- assumptions line --- */

/** The assumption block on its own, for a pane header that has several figures. */
export function Assumptions({ assumptions, source }: { assumptions: readonly AssumptionLine[]; source?: string }): ReactElement {
  return (
    <p
      data-assumption-line
      style={{
        ...T_MICRO,
        color: 'var(--color-ink-faint)',
        display: 'flex',
        gap: 6,
        flexWrap: 'wrap',
        alignItems: 'baseline',
      }}
    >
      <span>
        assumes{' '}
        {assumptions.map((line) => (
          <span key={line.key} style={{ whiteSpace: 'nowrap' }} title={line.note ?? undefined}>
            {line.key} {String(line.value)}{' '}
          </span>
        ))}
      </span>
      {source !== undefined ? <span>· {source}</span> : null}
    </p>
  );
}

/* --------------------------------------------------------- provenance ----- */

const PROVENANCE_LABEL: Record<string, string> = {
  pipeline: 'pipeline run',
  'null-adapter': 'null adapters, out/ on disk',
  'offline-snapshot': 'restored demo snapshot',
};

export function ProvenanceBadge({ provenance }: { provenance: string | null }): ReactElement {
  const key = provenance?.split(':')[0] ?? '';
  const isFixture = key === 'fixture';
  const label = isFixture
    ? `fixture data — ${provenance ?? ''} · not a pipeline run`
    : (PROVENANCE_LABEL[key] ?? (provenance ?? 'provenance not reported'));
  return (
    <span
      data-provenance={provenance ?? 'unknown'}
      style={{
        ...T_MICRO,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '2px 8px',
        border: `1px solid ${isFixture ? 'var(--color-state-failed)' : 'var(--color-hairline-strong)'}`,
        borderRadius: 'var(--radius-control)',
        color: isFixture ? 'var(--color-state-failed)' : 'var(--color-ink-muted)',
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
      }}
    >
      <Icon name={isFixture ? 'filter' : 'chain'} size={12} />
      {label}
    </span>
  );
}

/* ------------------------------------------------------------ degraded ---- */

export function DegradedBanner({
  dependency,
  fallback,
  meta,
}: {
  dependency: string;
  /** The deterministic path still in use, named from the response, not invented here. */
  fallback: string;
  meta: Meta;
}): ReactElement {
  return (
    <div
      role="status"
      aria-live="polite"
      data-degraded={dependency}
      style={{
        ...PANEL_SUNKEN,
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: '8px 12px',
        borderLeft: '2px solid var(--color-state-running)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ color: 'var(--color-state-running)' }}>
          <Icon name="dormant-wake" size={14} title="Degraded" />
        </span>
        <strong style={{ ...T_LABEL, color: 'var(--color-ink)', fontWeight: 600 }}>
          Degraded: {dependency} unavailable — showing {fallback}
        </strong>
      </div>
      <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
        {meta.degraded_reason ?? 'The pane is rendering the deterministic fallback rather than failing, and every figure below carries that fallback’s label.'}
      </p>
    </div>
  );
}

/* --------------------------------------------------------------- misc ----- */

export function Truncated({ text, className }: { text: string; className?: string }): ReactElement {
  return (
    <span className={className} title={text} style={{ ...ELLIPSIS, display: 'block' }}>
      {text}
    </span>
  );
}

export function Meter({ share, band }: { share: number; band?: Band }): ReactElement {
  const pct = Math.round(Math.min(Math.max(share, 0), 1) * 100);
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-block',
        width: '100%',
        height: 6,
        background: 'var(--color-hairline)',
        borderRadius: 'var(--radius-cell)',
        overflow: 'hidden',
      }}
    >
      <span
        style={{
          display: 'block',
          height: '100%',
          width: `${pct}%`,
          background: band === undefined ? 'var(--color-ink-muted)' : `var(--color-band-${band.toLowerCase()})`,
        }}
      />
    </span>
  );
}


export function Hairline({ label }: { label: string }): ReactElement {
  return (
    <p
      style={{
        ...T_MICRO,
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
        color: 'var(--color-ink-faint)',
        margin: '0 0 8px',
      }}
    >
      {label}
    </p>
  );
}
