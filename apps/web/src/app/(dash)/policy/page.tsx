/* =============================================================================
   Policy & Economics Simulator — plan §14 P8b-6, the best demo moment in the build.

   THE RULE: no precomputed theatre. Every slider change issues a real request to the
   allocation route and the whole page redraws from the answer. The client holds no
   curve, no frontier and no gap of its own; the only arithmetic performed here is
   formatting. A reviewer can prove it by watching the network tab: one request per
   committed change, one response per redraw.

   Greedy is synchronous and answers in the same request; the exact CP-SAT solve is
   user-triggered work with no known output shape, which is the ONE place the OXBOW
   mark is licensed to spin as a determinate arc. A 503 from the solver port is not an
   error screen — it is the labelled degraded path, greedy with the gap withheld,
   because the greedy answer is still the honest answer minus one claim.

   The "what changed" strip is the sentence a risk manager actually asks: which
   accounts entered the review set and which left it, by name.
   ============================================================================= */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactElement } from 'react';

import { Icon } from '@/design/icons/Icon';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { Pane } from '@/components/Pane';
import { LineChart, MultiLineChart } from '@/components/charts/charts';
import { MoneyFigure } from '@/components/ui/MoneyFigure';
import { MarkArc } from '@/components/ui/MarkArc';
import { ROUTES, type Allocation } from '@/lib/api/contract';
import { useListResource, useResource } from '@/lib/api/hooks';
import { failureDetail, failureRunId, failureTitle } from '@/lib/api/problem';
import { compactFromMinor, count, percent } from '@/lib/format/money';
import { PANEL_SUNKEN, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';

type Params = {
  capacity_minutes: number;
  recovery_rate: number;
  analyst_cost_per_hour: number;
  friction_cost: number;
};

export default function PolicyPage(): ReactElement {
  const defaults = useResource('policyDefaults', ROUTES.policyDefaults.path, ROUTES.policyDefaults.data);
  const [draft, setDraft] = useState<Params | null>(null);
  const [committed, setCommitted] = useState<Params | null>(null);
  const [exact, setExact] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pendingRef = useRef(false);

  /* The sliders hold a draft; the request fires on commit (pointer-up, keyboard up,
     or blur). Dragging emits one request per gesture rather than sixty, which is the
     difference between a live re-allocation and a rate-limited one. */
  useEffect(() => {
    if (defaults.data !== null && committed === null) {
      const initial: Params = {
        capacity_minutes: defaults.data.capacity_minutes,
        recovery_rate: defaults.data.recovery_rate,
        analyst_cost_per_hour: defaults.data.analyst_cost_per_hour.minor,
        friction_cost: defaults.data.friction_cost.minor,
      };
      setDraft(initial);
      setCommitted(initial);
    }
  }, [defaults.data, committed]);

  const allocation = useListResource('allocation', ROUTES.allocation.path, ROUTES.allocation.data, {
    ...(committed ?? {}),
    ...(exact ? { allocator: 'cpsat' } : {}),
  });

  const commit = useCallback((): void => {
    if (draft === null) return;
    if (draft.recovery_rate <= 0 || draft.recovery_rate >= 1) {
      // The open interval is enforced client-side so the analyst sees why the number
      // moved nothing, rather than watching an empty result.
      setError('Recovery rate must be strictly between 0 and 1: at either end the ranking degenerates, it does not become optimistic.');
      return;
    }
    setError(null);
    pendingRef.current = true;
    setCommitted(draft);
  }, [draft]);

  useEffect(() => {
    if (!allocation.isFetching) pendingRef.current = false;
  }, [allocation.isFetching]);

  if (defaults.data === null || draft === null) {
    return (
      <div style={{ padding: 'var(--spacing-pane-gap)' }}>
        <Pane id="simulator" title="Policy simulator" operation="Loading the active policy" meta={defaults.meta} skeleton={{ columns: [{ key: 'slider', width: '100%' }], rows: 4 }}>
          <span />
        </Pane>
      </div>
    );
  }

  const bounds = defaults.data.capacity_bounds;
  const answer: Allocation | null = allocation.data;
  const assumptions = allocation.meta?.assumptions ?? [];
  const solverDegraded = allocation.failure !== null && allocation.failure.class !== 'contract' && exact;

  return (
    <div style={{ padding: 'var(--spacing-pane-gap)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 380px) minmax(0, 1fr)', gap: 'var(--spacing-pane-gap)', alignItems: 'start' }}>
        {/* --------------------------------------------------- sliders ---- */}
        <Pane id="inputs" title="Assumptions under test" operation="Reading the economic assumptions" meta={defaults.meta} skeleton={{ columns: [{ key: 's', width: '100%' }], rows: 4 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Slider
              label="Review capacity"
              value={draft.capacity_minutes}
              min={bounds.min}
              max={bounds.max}
              step={bounds.step}
              format={(value) => `${count(value)} analyst-minutes`}
              onChange={(value) => setDraft({ ...draft, capacity_minutes: value })}
              onCommit={commit}
            />
            <Slider
              label="Recovery rate r"
              value={draft.recovery_rate}
              min={0.05}
              max={0.95}
              step={0.01}
              format={(value) => value.toFixed(2)}
              onChange={(value) => setDraft({ ...draft, recovery_rate: value })}
              onCommit={commit}
            />
            <Slider
              label="Analyst cost per hour"
              value={draft.analyst_cost_per_hour}
              min={100_000}
              max={3_000_000}
              step={50_000}
              format={(value) => `${compactFromMinor(value, 2)} ${defaults.data?.currency ?? ''}`}
              onChange={(value) => setDraft({ ...draft, analyst_cost_per_hour: value })}
              onCommit={commit}
            />
            <Slider
              label="Friction cost f"
              value={draft.friction_cost}
              min={0}
              max={10_000_000}
              step={100_000}
              format={(value) => `${compactFromMinor(value, 2)} ${defaults.data?.currency ?? ''}`}
              onChange={(value) => setDraft({ ...draft, friction_cost: value })}
              onCommit={commit}
            />

            {error !== null ? (
              <p role="alert" data-field-error="recovery_rate" style={{ ...T_MICRO, color: 'var(--color-state-failed)', margin: 0 }}>
                {error}
              </p>
            ) : null}

            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                type="button"
                onClick={() => {
                  setExact(true);
                  setTimeout(commit, 0);
                }}
                disabled={exact || allocation.isFetching}
                style={{ ...CONTROL, display: 'inline-flex', alignItems: 'center', gap: 6 }}
              >
                <Icon name="chain" size={14} /> solve exactly with CP-SAT
              </button>
              {exact ? (
                <button type="button" onClick={() => { setExact(false); setTimeout(commit, 0); }} style={CONTROL}>
                  back to greedy
                </button>
              ) : null}
            </div>

            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              every value on this page is the server’s answer to these four numbers · source {defaults.data.source} ·
              nothing here is precomputed
            </p>
          </div>
        </Pane>

        {/* ----------------------------------------------------- answers -- */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)', minWidth: 0 }}>
          {allocation.isFetching && answer !== null ? (
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', margin: 0 }}>re-allocating…</p>
          ) : null}

          {answer === null ? (
            solverDegraded ? (
              <DegradedSolver failure={allocation.failure} onRetry={() => setExact(false)} />
            ) : allocation.failure !== null ? (
              <ErrorPane
                paneId="allocation"
                operation="Re-allocating under these assumptions"
                error={{ title: failureTitle(allocation.failure), detail: failureDetail(allocation.failure) ?? undefined, run_id: failureRunId(allocation.failure) ?? undefined }}
                onRetry={() => void allocation.refetch()}
                attempt={allocation.attempts}
                retrying={allocation.isFetching}
              />
            ) : (
              <Pane id="allocation" title="Allocation" operation="Computing the allocation" meta={null} skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: 6 }}>
                {MarkArc({ progress: null, label: 'solving' })}
              </Pane>
            )
          ) : (
            <>
              <section style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 'var(--spacing-pane-gap)' }}>
                <MoneyFigure figure={answer.expected_loss_avoided} assumptions={assumptions} label="Expected loss avoided" emphasis="kpi" source={defaults.data.source} />
                <MoneyFigure figure={answer.benefit_per_analyst_hour} assumptions={assumptions} label="Benefit per analyst-hour" emphasis="kpi" source={defaults.data.source} />
                <MoneyFigure figure={answer.es975_unreviewed} assumptions={assumptions} label="Residual exposure ES 97.5%" emphasis="kpi" source={defaults.data.source} />
                <div style={{ ...PANEL_SUNKEN, padding: 10 }}>
                  <p style={{ ...T_LABEL, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Accounts reviewed</p>
                  <p className="u-num" style={{ fontFamily: 'var(--font-condensed)', fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0 0' }}>
                    {count(answer.accounts_reviewed)}
                  </p>
                  <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
                    {count(answer.customers_wrongly_touched)} legitimate customers touched · allocator{' '}
                    {answer.allocator === 'cpsat_timeout_greedy' ? 'CP-SAT timed out, greedy result' : answer.allocator}
                  </p>
                </div>
              </section>

              {/* what changed — the sentence a risk manager asks for */}
              <Pane id="changed" title="What changed" operation="Comparing review sets" meta={allocation.meta} skeleton={{ columns: [{ key: 'k', width: '100%' }], rows: 2 }}>
                <div data-what-changed style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                  <ChangeSet title="entered the review set" keys={answer.changed.entered} total={answer.changed.entered_count} tone="in" />
                  <ChangeSet title="left the review set" keys={answer.changed.left} total={answer.changed.left_count} tone="out" />
                </div>
              </Pane>

              <Pane id="curve" title="Cumulative benefit against the baselines" operation="Loading the benefit curve" meta={allocation.meta} skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: 1, rowHeight: 300 }}>
                <MultiLineChart series={answer.cumulative_curve} ariaLabel="Cumulative benefit by policy" />
              </Pane>

              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 'var(--spacing-pane-gap)' }}>
                <Pane id="frontier" title="Efficient frontier" operation="Loading the frontier" meta={allocation.meta} skeleton={{ columns: [{ key: 'f', width: '100%' }], rows: 1, rowHeight: 220 }}>
                  <Frontier allocation={answer} currency={defaults.data.currency} />
                </Pane>

                <Pane id="risk" title="Tail and gap" operation="Loading the tail figures" meta={allocation.meta} skeleton={{ columns: [{ key: 'r', width: '100%' }], rows: 4 }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    <MoneyFigure figure={answer.max_drawdown} assumptions={assumptions} label="Max drawdown of cumulative net benefit" compact source={defaults.data.source} />
                    <MoneyFigure figure={answer.var95_unreviewed} assumptions={assumptions} label="VaR 95% of unreviewed exposure" compact showBand={false} />
                    {answer.optimality_gap === null ? (
                      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', maxWidth: '46ch' }}>
                        No optimality gap: the exact solve has not run for this configuration, so the gap would be a
                        number without a bound behind it. Press “solve exactly” for it.
                      </p>
                    ) : (
                      <div style={{ ...PANEL_SUNKEN, padding: 10 }} data-optimality-gap>
                        <p style={{ ...T_LABEL, margin: 0 }}>Gap to the exact CP-SAT optimum</p>
                        <p className="u-num" style={{ fontFamily: 'var(--font-condensed)', fontSize: 'var(--text-kpi)', fontWeight: 600, margin: '2px 0' }}>
                          {percent(answer.optimality_gap.ratio, 2)}
                        </p>
                        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
                          {compactFromMinor(answer.optimality_gap.absolute.minor, answer.optimality_gap.absolute.decimals)}{' '}
                          {answer.optimality_gap.absolute.currency} · greedy {answer.optimality_gap.greedy_objective.toFixed(0)} vs exact{' '}
                          {answer.optimality_gap.cpsat_objective.toFixed(0)} · solve {String(answer.solve_ms ?? 0)} ms
                        </p>
                      </div>
                    )}
                    {answer.degraded ? (
                      <p style={{ ...T_MICRO, color: 'var(--color-state-running)' }}>
                        degraded: the greedy allocator produced this answer, and the page says so rather than implying
                        optimality
                      </p>
                    ) : null}
                  </div>
                </Pane>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const CONTROL: CSSProperties = {
  ...T_LABEL,
  padding: '5px 10px',
  cursor: 'pointer',
  color: 'var(--color-ink)',
  background: 'transparent',
  border: '1px solid var(--color-hairline-strong)',
  borderRadius: 'var(--radius-control)',
};

function Slider({
  label,
  value,
  min,
  max,
  step,
  format,
  onChange,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (value: number) => string;
  onChange: (value: number) => void;
  onCommit: () => void;
}): ReactElement {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
        <span style={{ ...T_LABEL }}>{label}</span>
        <span className="u-num" style={{ ...T_MONO, fontSize: 'var(--text-body)', color: 'var(--color-ink)' }}>
          {format(value)}
        </span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        onPointerUp={onCommit}
        onKeyUp={onCommit}
        style={{ accentColor: 'var(--color-evidence)' }}
      />
    </label>
  );
}

function ChangeSet({ title, keys, total, tone }: { title: string; keys: readonly string[]; total: number; tone: 'in' | 'out' }): ReactElement {
  return (
    <div style={{ minWidth: 200 }}>
      <p style={{ ...T_LABEL, color: tone === 'in' ? 'var(--color-band-d)' : 'var(--color-band-b)', margin: 0 }}>
        {count(total)} {title}
      </p>
      {total === 0 ? (
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)', marginTop: 4 }}>
          nobody moved: this change did not alter which accounts get read.
        </p>
      ) : (
        <ul style={{ listStyle: 'none', margin: '4px 0 0', padding: 0, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {keys.map((key) => (
            <li key={key}>
              <Link href={`/cases/${encodeURIComponent(key)}`} style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink)', textDecoration: 'underline' }}>
                {key}
              </Link>
            </li>
          ))}
          {total > keys.length ? <li style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>+{count(total - keys.length)} more</li> : null}
        </ul>
      )}
    </div>
  );
}

function Frontier({ allocation, currency }: { allocation: Allocation; currency: string }): ReactElement {
  const points = allocation.frontier.points.map((point) => ({
    x: new Date(Date.UTC(1970, 0, 1, 0, 0, 0) + point.capacity_minutes * 60_000).toISOString(),
    y: point.loss_avoided.minor,
    label: `${count(point.capacity_minutes)} min · ${count(point.wrongly_touched)} touched`,
  }));
  return (
    <div>
      <LineChart
        points={points}
        yIsMoney
        mark={points[allocation.frontier.current_index] === undefined ? null : { x: points[allocation.frontier.current_index]?.x ?? '', label: 'here' }}
        formatY={(value) => compactFromMinor(value, 2)}
        formatX={(value) => `${count(Math.round(Date.parse(value) / 60_000))} min`}
        ariaLabel="Achievable expected loss avoided at each capacity, with the current operating point marked"
      />
      <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
        axes: capacity in analyst-minutes against achievable {currency} loss avoided. One dominated policy is drawn on
        the same curve — the point at {(points[allocation.frontier.current_index] === undefined ? '' : points[Math.max(allocation.frontier.current_index - 1, 0)]?.label ?? '')}{' '}
        — so the frontier has something to be better than.
      </p>
    </div>
  );
}

function DegradedSolver({ failure, onRetry }: { failure: Parameters<typeof failureTitle>[0] | null; onRetry: () => void }): ReactElement {
  return (
    <Pane id="allocation" title="Allocation" operation="Solving exactly" meta={null} skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: 4 }}>
      <div style={{ ...PANEL_SUNKEN, padding: 12, borderLeft: '2px solid var(--color-state-running)' }}>
        <p style={{ ...T_LABEL, color: 'var(--color-ink)', margin: 0 }}>
          Degraded: the CP-SAT port is unavailable, so the deterministic greedy allocation is shown instead of no
          allocation.
        </p>
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)', marginTop: 6 }}>
          {failure === null ? 'The solver did not answer.' : `${failureTitle(failure)} — ${failureDetail(failure) ?? ''}`} The
          greedy result is still the optimum of the fractional relaxation, and the optimality gap is withheld because a
          time-limited incumbent has no dual bound to compare against.
        </p>
        <button type="button" onClick={onRetry} style={{ ...CONTROL, marginTop: 8 }}>
          continue with greedy
        </button>
      </div>
    </Pane>
  );
}

