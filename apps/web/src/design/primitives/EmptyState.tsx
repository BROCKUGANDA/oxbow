/* =============================================================================
   OXBOW EmptyState.
   Spec 12.6 state craft.

   Four distinct empty states, and NOT ONE of them says "No data available".

   That phrase is banned because it is a confession that the component has no
   idea why it is empty. Every one of these four cases knows exactly why the
   region is blank, and each says so in the user's terms and then offers the one
   action that changes it. An empty state is the cheapest place in the product to
   earn trust, and it is wasted on "nothing here".

   (a) filters-excluded   the narrowest predicate is named, removable in one click,
                          with the count that would come back
   (b) no-run             fresh install: the make command, copyable, with runtime
   (c) window-empty       the window is why the graph is blank, with hop and date
                          widening offered inline
   (d) no-disagreement    zero scorecard/GBM disagreements is a FINDING, framed as
                          one, with the threshold that would surface near-misses
   ============================================================================= */

import type { CSSProperties, ReactElement, ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { Icon } from '../icons/Icon';
import type { GlyphName } from '../icons/Icon';

/* ------------------------------------------------------------------ types -- */

export interface NarrowestPredicate {
  /** The filter field, in the user's words: "Amount is at least 500,000". */
  label: string;
  /** The current value, formatted for display. */
  value: string;
  /** What the results count would be if this one predicate were dropped. */
  rowsIfRemoved: number;
  /** Drops just this predicate. Must be a single reversible state change. */
  onRemove: () => void;
}

export interface FiltersExcludedState {
  kind: 'filters-excluded';
  /** The ONE predicate doing the damage. Not the whole filter set. */
  narrowest: NarrowestPredicate;
  /** Every other predicate, rendered as removable chips. */
  others?: readonly { label: string; value: string; onRemove: () => void }[];
  /** Total rows the unfiltered query returns, for contrast. */
  unfilteredRows: number;
}

export interface NoRunState {
  kind: 'no-run';
  /** Always the make target that produces data. */
  command: string;
  /** Real measured or documented runtime, so nobody waits blind. */
  expectedRuntime: string;
  /** Optional corpus note, e.g. which dataset the run ingests. */
  corpus?: string;
}

export interface WindowEmptyState {
  kind: 'window-empty';
  /** The account under investigation, already rendered as a chip by the caller. */
  accountId: string;
  /** ISO dates of the current window. */
  from: string;
  to: string;
  /** Edges found at the current hop count. Zero, by definition of this state. */
  edgesAtCurrentHops: number;
  /** What the wider window would return, if known. Null when it has not been tried. */
  edgesAtWiderWindow: number | null;
  currentHops: number;
  onWidenHops: (hops: number) => void;
  onWidenDates: (from: string, to: string) => void;
  maxHops: number;
}

export interface NoDisagreementState {
  kind: 'no-disagreement';
  /** The band above which the two models were compared. */
  bandAbove: string;
  /** How many accounts were compared. */
  comparedAccounts: number;
  /** Largest disagreement observed, as an absolute score delta. Zero here. */
  maxDelta: number;
  /** The threshold that would surface near-misses, with its rationale. */
  nearMissThreshold: number;
  /** How many accounts a threshold at that value would have flagged. */
  rowsAtThreshold: number;
  onSetThreshold: (value: number) => void;
}

/**
 * (e) no-cycles — the graph's own reason, and the one this product is judged on.
 *
 * A transaction graph can be full of accounts and edges and still contain no cycle.
 * That is not "no data" and it is not a failure: it is the measured result for at least
 * one of the two corpora (DEV-011 — PaySim, median counterparty degree 1.0, zero
 * surviving time-respecting 3–6 cycles), and it is the result a time-respecting filter
 * *ought* to produce on a corpus of one-hop transfers. Saying "no cycles found" would
 * throw the finding away and read like a broken page, so this state names the filter
 * that emptied it and what would have to change for anything to survive it.
 *
 * Every number here is a field the subgraph response already carries. The component is
 * not given, and must not be given, a "cycles found before the filter" figure: the
 * server does not publish one, and inventing one on a screen whose whole claim is
 * traceability would be the exact failure DESIGN.md §7 exists to prevent.
 */
export interface NoCyclesState {
  kind: 'no-cycles';
  accountsDrawn: number;
  edgesDrawn: number;
  /** The window the filter ran over, already zone-formatted by the caller. */
  windowFrom: string;
  windowTo: string;
  /** Hops actually traversed, and the server's ceiling for this route. */
  currentHops: number;
  maxHops: number;
  onWidenHops: (hops: number) => void;
  /** Drop the cycle overlay and draw the whole subgraph instead. */
  onShowAllEdges: () => void;
}

export type EmptyStateProps =
  | FiltersExcludedState
  | NoRunState
  | WindowEmptyState
  | NoDisagreementState
  | NoCyclesState;

export interface EmptyStateFrameProps {
  children: ReactNode;
  /** Constrains the panel. 6px radius, hairline border, no shadow. */
  maxWidth?: number;
  className?: string;
  style?: CSSProperties;
}

/* ----------------------------------------------------------------- shared -- */

/** Panel frame. Hairline border, 6px radius, zero blur. */
export function EmptyStateFrame({ children, maxWidth = 520, className, style }: EmptyStateFrameProps): ReactElement {
  return (
    <div
      className={className}
      style={{
        maxWidth,
        padding: '20px',
        border: '1px solid var(--color-hairline)',
        borderRadius: 'var(--radius-panel)',
        background: 'var(--color-canvas-raised)',
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function Headline({ children }: { children: ReactNode }): ReactElement {
  return (
    <p
      style={{
        margin: 0,
        fontFamily: 'var(--font-sans)',
        fontSize: '0.875rem',
        fontWeight: 600,
        color: 'var(--color-ink)',
      }}
    >
      {children}
    </p>
  );
}

function Body({ children }: { children: ReactNode }): ReactElement {
  return (
    <p
      style={{
        margin: '6px 0 0',
        fontFamily: 'var(--font-sans)',
        fontSize: '0.75rem',
        lineHeight: 1.5,
        color: 'var(--color-ink-muted)',
      }}
    >
      {children}
    </p>
  );
}

function Hint({ children }: { children: ReactNode }): ReactElement {
  return (
    <p
      style={{
        margin: '12px 0 0',
        fontFamily: 'var(--font-sans)',
        fontSize: '0.6875rem',
        lineHeight: 1.45,
        color: 'var(--color-ink-faint)',
      }}
    >
      {children}
    </p>
  );
}

/**
 * A dismiss cross, drawn inline rather than taken from the sprite.
 *
 * It is tiny, sits inside a text button, and inlines at 1.75px on the same grid
 * as everything else, so it needs no second HTTP request for one 9px mark. The
 * alternative the banned list rules out is a text glyph such as U+2715, which
 * is exactly the emoji-as-iconography tell.
 */
function Dismiss({ style }: { style?: React.CSSProperties }): ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      strokeLinecap="square"
      aria-hidden="true"
      focusable="false"
      style={style}
    >
      <path d="M6 6 L18 18" />
      <path d="M18 6 L6 18" />
    </svg>
  );
}

/** 3px control. 80ms on hover, easing per the motion rule. */
function Action({
  children,
  onClick,
  emphasis = false,
  disabled,
}: {
  children: ReactNode;
  onClick: () => void;
  emphasis?: boolean;
  disabled?: boolean;
}): ReactElement {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        fontFamily: 'var(--font-sans)',
        fontSize: '0.75rem',
        fontWeight: 500,
        color: emphasis ? 'var(--color-ink-inverse)' : 'var(--color-ink)',
        background: emphasis ? 'var(--color-ink)' : 'transparent',
        border: `1px solid ${emphasis ? 'var(--color-ink)' : 'var(--color-hairline-strong)'}`,
        borderRadius: 'var(--radius-control)',
        padding: '4px 10px',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'background var(--duration-instant) var(--ease-out-quint)',
      }}
    >
      {children}
    </button>
  );
}

/** Counts are tabular. Every single one. */
function Count({ value }: { value: number }): ReactElement {
  return (
    <span className="u-tabular" style={{ color: 'var(--color-ink)', fontWeight: 600 }}>
      {value.toLocaleString('en-US')}
    </span>
  );
}

function Row({ children }: { children: ReactNode }): ReactElement {
  return <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 }}>{children}</div>;
}

/* ------------------------------------------------------------- copy button -- */

/**
 * Copies text and says so for two seconds, then reverts. No toast library, no
 * dependency: the confirmation is the button's own label, which is where the
 * user's eyes already are.
 */
export function useCopyButton(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback((text: string) => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    void navigator.clipboard.writeText(text);
    setCopied(true);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2000);
  }, []);

  return [copied, copy];
}

export function CopyButton({ text, label }: { text: string; label: string }): ReactElement {
  const [copied, copy] = useCopyButton();
  return (
    <Action onClick={() => copy(text)}>
      {copied ? 'Copied' : label}
    </Action>
  );
}

/* ------------------------------------------------------------------ union -- */

export function EmptyState(props: EmptyStateProps): ReactElement {
  switch (props.kind) {
    case 'filters-excluded':
      return <FiltersExcluded {...props} />;
    case 'no-run':
      return <NoRun {...props} />;
    case 'window-empty':
      return <WindowEmpty {...props} />;
    case 'no-disagreement':
      return <NoDisagreement {...props} />;
    case 'no-cycles':
      return <NoCycles {...props} />;
  }
}

function Glyph({ name, title }: { name: GlyphName; title: string }): ReactElement {
  return <Icon name={name} size={16} title={title} style={{ color: 'var(--color-ink-faint)' }} />;
}

/* (a) filters exclude everything ------------------------------------------- */

function FiltersExcluded(props: FiltersExcludedState): ReactElement {
  const { narrowest, others, unfilteredRows } = props;
  return (
    <EmptyStateFrame>
      <Glyph name="filter" title="Filters" />
      <div style={{ marginTop: 10 }}>
        <Headline>Your filters exclude all {unfilteredRows.toLocaleString('en-US')} alerts</Headline>
        <Body>
          The narrowest predicate is <strong style={{ color: 'var(--color-ink)' }}>{narrowest.label}</strong>, currently{' '}
          <span className="u-tabular">{narrowest.value}</span>. Removing just that one returns{' '}
          <Count value={narrowest.rowsIfRemoved} /> alerts.
        </Body>
      </div>

      <Row>
        <Action onClick={narrowest.onRemove} emphasis>
          Remove “{narrowest.label}”
        </Action>
      </Row>

      {others && others.length > 0 ? (
        <div style={{ marginTop: 14 }}>
          <p
            style={{
              margin: 0,
              fontSize: '0.6875rem',
              color: 'var(--color-ink-faint)',
              fontFamily: 'var(--font-sans)',
            }}
          >
            Other predicates still applied
          </p>
          <Row>
            {others.map((other) => (
              <Action key={other.label} onClick={other.onRemove}>
                {other.label}: {other.value}
                <Dismiss
                  style={{
                    width: 9,
                    height: 9,
                    marginLeft: 6,
                    verticalAlign: -1,
                    opacity: 0.7,
                  }}
                />
              </Action>
            ))}
          </Row>
        </div>
      ) : null}

      <Hint>
        The queue is not empty. It is unreachable through this filter combination, and the count above is what it is
        hiding.
      </Hint>
    </EmptyStateFrame>
  );
}

/* (b) fresh install, no run yet -------------------------------------------- */

function NoRun(props: NoRunState): ReactElement {
  return (
    <EmptyStateFrame>
      <Glyph name="chain" title="Pipeline" />
      <div style={{ marginTop: 10 }}>
        <Headline>No run recorded yet</Headline>
        <Body>
          This install has never built a graph or scored an account
          {props.corpus ? ` from ${props.corpus}` : ''}. The pipeline is four stages and is separately resumable, so a
          failure at stage three does not throw away stages one and two.
        </Body>
      </div>

      <div
        style={{
          marginTop: 12,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          padding: '8px 10px',
          border: '1px solid var(--color-hairline)',
          borderRadius: 'var(--radius-control)',
          background: 'var(--color-canvas-sunken)',
        }}
      >
        <code
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '0.75rem',
            color: 'var(--color-ink)',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {props.command}
        </code>
        <CopyButton text={props.command} label="Copy" />
      </div>

      <Hint>
        Expect about {props.expectedRuntime}. Stage events stream to this page as they complete, so you can watch the
        ledger fill rather than guess whether it started.
      </Hint>
    </EmptyStateFrame>
  );
}

/* (c) account has no counterparties in the window -------------------------- */

function WindowEmpty(props: WindowEmptyState): ReactElement {
  const { accountId, from, to, currentHops, maxHops, edgesAtCurrentHops, edgesAtWiderWindow, onWidenHops, onWidenDates } =
    props;

  /* One month either side is the honest first widening: enough to catch a
     counterparty that straddles the boundary, still narrow enough to be an
     investigation rather than a fishing expedition. */
  const widened = widenByDays(from, to, 30);

  return (
    <EmptyStateFrame>
      <Glyph name="embargo" title="Window" />
      <div style={{ marginTop: 10 }}>
        <Headline>{accountId} has no counterparties between {from} and {to}</Headline>
        <Body>
          The graph is empty because the <strong style={{ color: 'var(--color-ink)' }}>window</strong> is narrow, not
          because the account is isolated. At {currentHops} hop{currentHops === 1 ? '' : 's'} this account has{' '}
          <Count value={edgesAtCurrentHops} /> edges in range. An account that transacted and then went quiet reads
          exactly like one that never transacted at all unless the window is stated out loud.
        </Body>
      </div>

      <Row>
        {currentHops < maxHops ? (
          <Action onClick={() => onWidenHops(Math.min(currentHops + 1, maxHops))} emphasis>
            Widen to {Math.min(currentHops + 1, maxHops)} hop{Math.min(currentHops + 1, maxHops) === 1 ? '' : 's'}
          </Action>
        ) : null}
        <Action onClick={() => onWidenDates(widened.from, widened.to)}>
          Widen to {widened.from} → {widened.to}
        </Action>
      </Row>

      <Hint>
        {edgesAtWiderWindow === null ? (
          <>Widening is untested for this account. Every result is marked with the window that produced it.</>
        ) : (
          <>
            The wider window returns <Count value={edgesAtWiderWindow} /> edges for this account.
          </>
        )}{' '}
        Widen the window before concluding the account is clean — absence of evidence in a narrow window is a property
        of the window, not of the account.
      </Hint>
    </EmptyStateFrame>
  );
}

function widenByDays(from: string, to: string, days: number): { from: string; to: string } {
  const widen = (iso: string, deltaDays: number): string => {
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return iso;
    parsed.setUTCDate(parsed.getUTCDate() + deltaDays);
    return parsed.toISOString().slice(0, 10);
  };
  return { from: widen(from, -days), to: widen(to, days) };
}

/* (d) zero model disagreements --------------------------------------------- */

function NoDisagreement(props: NoDisagreementState): ReactElement {
  return (
    <EmptyStateFrame>
      <Glyph name="cycle" title="Agreement" />
      <div style={{ marginTop: 10 }}>
        <Headline>The two models agree on every account above band {props.bandAbove}</Headline>
        <Body>
          Across <Count value={props.comparedAccounts} /> accounts, the scorecard and the GBM produced no
          disagreements, and the largest gap was <Count value={props.maxDelta} />. That is a finding, not an absence
          of one: two independent models converging on the same ranking is the strongest evidence the feature set
          carries, and a page that renders nothing here is throwing that away.
        </Body>
      </div>

      <div
        style={{
          marginTop: 12,
          padding: '8px 10px',
          border: '1px solid var(--color-hairline)',
          borderRadius: 'var(--radius-control)',
          background: 'var(--color-canvas-sunken)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
        }}
      >
        <span style={{ fontSize: '0.75rem', color: 'var(--color-ink-muted)', fontFamily: 'var(--font-sans)' }}>
          Surface near-misses at a delta of{' '}
          <span className="u-tabular" style={{ color: 'var(--color-ink)', fontWeight: 600 }}>
            {props.nearMissThreshold}
          </span>
        </span>
        <span style={{ fontSize: '0.75rem', color: 'var(--color-ink-faint)', fontFamily: 'var(--font-sans)' }}>
          <Count value={props.rowsAtThreshold} /> would appear
        </span>
      </div>

      <Row>
        <Action onClick={() => props.onSetThreshold(props.nearMissThreshold)} emphasis>
          Show near-misses at {props.nearMissThreshold}
        </Action>
      </Row>

      <Hint>
        The threshold is a reporting choice, not a fitted parameter. Raising it loosens the comparison; it never
        changes either model&apos;s scores.
      </Hint>
    </EmptyStateFrame>
  );
}

export default EmptyState;

/* (e) no time-respecting cycles survived ----------------------------------- */

function NoCycles(props: NoCyclesState): ReactElement {
  const atCeiling = props.currentHops >= props.maxHops;
  return (
    <EmptyStateFrame>
      <Glyph name="cycle" title="No cycles" />
      <div style={{ marginTop: 10 }}>
        <Headline>No cycle survived the filters over this window</Headline>
        <Body>
          The explorer drew <Count value={props.accountsDrawn} /> accounts and{' '}
          <Count value={props.edgesDrawn} /> edges between {props.windowFrom} and {props.windowTo}, and every
          candidate loop was rejected for being out of order in time or for losing value along the way. That is a
          measurement of this corpus, not a query that failed: a cycle here has to return to its origin, hand
          money forward at each hop, and do both inside the window.
        </Body>
      </div>

      <div
        style={{
          marginTop: 12,
          padding: '8px 10px',
          border: '1px solid var(--color-hairline)',
          borderRadius: 'var(--radius-control)',
          background: 'var(--color-canvas-sunken)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
        }}
      >
        <span style={{ fontSize: '0.75rem', color: 'var(--color-ink-muted)', fontFamily: 'var(--font-sans)' }}>
          Traversed <span className="u-tabular" style={{ color: 'var(--color-ink)', fontWeight: 600 }}>{props.currentHops}</span>{' '}
          of up to <span className="u-tabular" style={{ color: 'var(--color-ink)', fontWeight: 600 }}>{props.maxHops}</span> hops
        </span>
        <span style={{ fontSize: '0.75rem', color: 'var(--color-ink-faint)', fontFamily: 'var(--font-sans)' }}>
          Wider windows are set on the query, not here
        </span>
      </div>

      <Row>
        {atCeiling ? null : (
          <Action onClick={() => props.onWidenHops(props.maxHops)} emphasis>
            Re-run at {props.maxHops} hops
          </Action>
        )}
        <Action onClick={props.onShowAllEdges}>Show the subgraph without the overlay</Action>
      </Row>

      <Hint>
        Dropping the overlay shows the topology that was actually found. A two-hop round trip between one account
        and itself is excluded on purpose: it is a reinvestment, not a network.
      </Hint>
    </EmptyStateFrame>
  );
}
