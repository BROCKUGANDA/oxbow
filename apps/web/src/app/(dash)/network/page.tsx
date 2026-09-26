/* =============================================================================
   Network explorer — plan §14 P8b-4.

   Cytoscape renders; fcose lays out. Both are loaded client-side only, because a
   canvas graph has no meaningful server render and importing it into the RSC graph
   would ship it to the browser twice.

   The encoding rules are the risk-legibility rules again, and they are all three
   active at once on a node: size by exposure, colour by band, BORDER by community.
   A third channel for community is not decoration — at 1,500 nodes a hue-only
   grouping is unreadable for anyone with any colour vision deficiency, and the
   greyscale packet still has to say which cluster an account belongs to.

   The cap is enforced by the server (plan §7) and mirrored here: beyond 1,500 nodes
   a community collapses into a meta-node whose label is its TRUE size, so a shrunk
   graph never reads as a small one.

   The time scrubber replays edge formation by toggling edge classes per frame — no
   re-layout, no refetch, and the layout the analyst is looking at stays stable.
   ============================================================================= */

'use client';

import dynamic from 'next/dynamic';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useMemo, useState, type ReactElement } from 'react';

import { EmptyState } from '@/design/primitives/EmptyState';
import { Shimmer } from '@/design/primitives/Shimmer';
import { Icon } from '@/design/icons/Icon';
import { Pane } from '@/components/Pane';
import { BandBadge } from '@/components/ui/BandBadge';
import { AccountChip } from '@/components/ui/provenance';
import { TYPOLOGY_META, glyphFor } from '@/components/typology';
import { ROUTES, type GraphEdge, type GraphNode, type Subgraph } from '@/lib/api/contract';
import { useListResource, useRuntime } from '@/lib/api/hooks';
import { compactFromMinor, count } from '@/lib/format/money';
import { formatDate } from '@/lib/format/time';
import { PANEL_SUNKEN, T_LABEL, T_MICRO, T_MONO } from '@/components/ui/sx';

/** The explorer canvas. `ssr: false` — see the note above. */
const GraphCanvas = dynamic(() => import('./canvas').then((module) => module.GraphCanvas), {
  ssr: false,
  loading: () => (
    <div
      aria-hidden="true"
      style={{ height: '100%', minHeight: 420, background: 'var(--color-canvas-sunken)', borderRadius: 'var(--radius-cell)' }}
    />
  ),
});

type Overlay = 'cycles' | 'velocity' | 'fans' | 'communities' | 'flagged';

/** THE canvas height, shared by the resolved layout and by both skeletons. One constant
 *  is the only reason the claim "the skeleton matches the resolved geometry" is true
 *  rather than merely intended — a second literal would drift the moment one moved. */
const CANVAS_HEIGHT = 'min(62vh, 640px)';

const OVERLAYS: readonly { id: Overlay; label: string; glyph: 'cycle' | 'velocity-spike' | 'fan-in' | 'chain' | 'hash-link' }[] = [
  { id: 'cycles', label: 'cycles', glyph: 'cycle' },
  { id: 'velocity', label: 'high-velocity hops', glyph: 'velocity-spike' },
  { id: 'fans', label: 'fan stars', glyph: 'fan-in' },
  { id: 'communities', label: 'dense communities', glyph: 'chain' },
  { id: 'flagged', label: 'flagged nodes', glyph: 'hash-link' },
];

/* The route reads its query string, and App Router can only do that on the client.
   Without a Suspense boundary above the reader the whole segment opts out of static
   rendering and the build fails; with an unshaped fallback it would resolve into a
   jump. So the fallback reserves the explorer's exact geometry — canvas column, side
   rail, scrubber strip — and the resolved page drops into it. CLS stays zero. */
export default function NetworkPage(): ReactElement {
  return (
    <Suspense fallback={<NetworkSkeleton />}>
      <NetworkExplorer />
    </Suspense>
  );
}

/* The explorer's own geometry, reserved before the query string is readable: canvas
   column at the same height the canvas resolves to, the scrubber strip beneath it, the
   overlay row beneath that, and the 320 px rail beside all three. Nothing below the
   fold moves when the boundary resolves. */
function NetworkSkeleton(): ReactElement {
  const rail = [
    { key: 'controls', rows: 3 },
    { key: 'overlay-counts', rows: 5 },
  ];
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) 320px',
        gap: 'var(--spacing-pane-gap)',
        padding: 'var(--spacing-pane-gap)',
        alignItems: 'start',
      }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        <Pane id="graph" title="Network" operation="Loading the subgraph" skeleton={{ columns: [{ key: 'canvas', width: '100%' }], rows: 1 }}>
          <div style={{ height: CANVAS_HEIGHT }} aria-hidden="true">
            <Shimmer width="100%" height="100%" radius="var(--radius-cell)" />
          </div>
        </Pane>
        <div style={{ ...PANEL_SUNKEN, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 10 }}>
          <Shimmer width="100%" height={14} />
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {OVERLAYS.map((overlay) => (
            <Shimmer key={overlay.id} width={112} height={22} radius="var(--radius-control)" />
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)' }}>
        {rail.map((entry) => (
          <Pane
            key={entry.key}
            id={entry.key}
            title={entry.key === 'controls' ? 'Subgraph query' : 'What the overlays select'}
            operation="Loading the explorer rail"
            skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: entry.rows }}
          >
            <span />
          </Pane>
        ))}
      </div>
    </div>
  );
}

function NetworkExplorer(): ReactElement {
  const params = useSearchParams();
  const runtime = useRuntime();
  const root = params.get('account') ?? '';
  const hops = Math.min(Math.max(Number(params.get('hops') ?? '2'), 1), 4);
  const minAmount = Number(params.get('min_minor') ?? '0');
  const stress = Number(params.get('nodes') ?? '0');

  const [active, setActive] = useState<Overlay[]>(['cycles', 'flagged']);
  const [frame, setFrame] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const graph = useListResource('subgraph', ROUTES.subgraph.path, ROUTES.subgraph.data, {
    account: root,
    hops,
    min_minor: minAmount,
    ...(stress > 0 ? { nodes: stress } : {}),
  });

  const data = graph.data;
  const buckets = data?.edges_by_bucket ?? [];
  const frameIndex = frame === null ? buckets.length : Math.min(frame, buckets.length);

  /* The visible subgraph at the scrubber's frame. Edges appear in bucket order, which
     is how the replay reads as the network assembling rather than as a fade. */
  const visible = useMemo(() => {
    if (data === null) return null;
    if (frame === null) return data;
    const cutoff = buckets[Math.max(frameIndex - 1, 0)]?.bucket ?? data.window.to;
    const edges = data.edges.filter((edge) => edge.ts_first <= cutoff);
    const live = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
    return { ...data, edges, nodes: data.nodes.filter((node) => live.has(node.key) || node.hops === 0) };
  }, [data, frame, frameIndex, buckets]);

  const toggle = (id: Overlay): void =>
    setActive((current) => (current.includes(id) ? current.filter((entry) => entry !== id) : [...current, id]));

  const publish = useCallback(
    (next: Record<string, string>) => {
      const merged = new URLSearchParams(params?.toString() ?? '');
      for (const [key, value] of Object.entries(next)) merged.set(key, value);
      window.history.replaceState(null, '', `${window.location.pathname}?${merged.toString()}`);
    },
    [params],
  );

  if (data === null) {
    return (
      <div style={{ padding: 'var(--spacing-pane-gap)' }}>
        <Pane id="graph" title="Network" operation="Loading the subgraph" meta={graph.meta} skeleton={{ columns: [{ key: 'canvas', width: '100%' }], rows: 12, rowHeight: 36 }}>
          <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
            {graph.failure === null ? 'Reading the subgraph the run built for this account.' : 'The explorer pane failed; the rest of the page is unaffected.'}
          </p>
        </Pane>
      </div>
    );
  }

  const empty = visible !== null && visible.edges.length === 0;

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 'var(--spacing-pane-gap)', padding: 'var(--spacing-pane-gap)', alignItems: 'start' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        <Pane
          id="graph"
          title="Network"
          operation="Drawing the subgraph"
          meta={graph.meta}
          actions={
            <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              {count(visible?.nodes.length ?? 0)} nodes · {count(visible?.edges.length ?? 0)} edges · cap{' '}
              {count(data.cap)}
            </span>
          }
          padded={false}
          skeleton={{ columns: [{ key: 'canvas', width: '100%' }], rows: 12, rowHeight: 36 }}
        >
          {empty ? (
            <div style={{ padding: 'var(--spacing-pane-gap)' }}>
              <EmptyState
                kind="window-empty"
                accountId={root === '' ? 'the selected root' : root}
                from={formatDate(data.window.from, runtime.data?.deployment_timezone ?? '')}
                to={formatDate(data.window.to, runtime.data?.deployment_timezone ?? '')}
                edgesAtCurrentHops={data.edges.length}
                edgesAtWiderWindow={null}
                currentHops={hops}
                maxHops={4}
                onWidenHops={(next) => publish({ hops: String(next) })}
                onWidenDates={() => undefined}
              />
            </div>
          ) : (
            <div style={{ height: CANVAS_HEIGHT, position: 'relative' }}>
              <GraphCanvas
                nodes={visible?.nodes ?? []}
                edges={visible?.edges ?? []}
                overlays={active}
                selected={selected}
                onSelect={setSelected}
                truncated={data.truncated}
              />
            </div>
          )}
        </Pane>

        {/* Scrubber: real bucket boundaries from the response, so the replay is the
            server's edge-formation order and not an invented animation. */}
        <div data-print-hide style={{ ...PANEL_SUNKEN, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ ...T_MICRO, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--color-ink-faint)' }}>
            time scrubber
          </span>
          <input
            type="range"
            min={0}
            max={buckets.length}
            value={frame === null ? buckets.length : frame}
            onChange={(event) => setFrame(Number(event.target.value) === buckets.length ? null : Number(event.target.value))}
            aria-label="Replay edge formation up to a point in the window"
            style={{ flex: 1, minWidth: 160, accentColor: 'var(--color-evidence)' }}
          />
          <span className="u-num" style={{ ...T_MICRO, color: 'var(--color-ink-muted)', minWidth: 150 }}>
            {frame === null
              ? 'whole window'
              : `edges formed to ${buckets[Math.max(frameIndex - 1, 0)]?.bucket?.slice(0, 10) ?? '—'}`}
          </span>
        </div>

        <div data-print-hide style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>overlays</span>
          {OVERLAYS.map((overlay) => {
            const on = active.includes(overlay.id);
            return (
              <button
                key={overlay.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(overlay.id)}
                style={{
                  ...T_MICRO,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 5,
                  padding: '2px 8px',
                  cursor: 'pointer',
                  color: 'var(--color-ink)',
                  background: on ? 'var(--color-elev-2)' : 'transparent',
                  border: `1px solid ${on ? 'var(--color-evidence)' : 'var(--color-hairline-strong)'}`,
                  borderRadius: 'var(--radius-control)',
                }}
              >
                <Icon name={overlay.glyph} size={13} />
                {overlay.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* ------------------------------------------------------- side rail */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-pane-gap)' }}>
        <Pane id="controls" title="Subgraph query" operation="Adjusting the subgraph" meta={graph.meta} skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: 3 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <label style={{ ...T_LABEL, display: 'flex', flexDirection: 'column', gap: 4 }}>
              hops · {String(hops)}
              <input
                type="range"
                min={1}
                max={4}
                value={hops}
                onChange={(event) => publish({ hops: event.target.value })}
                style={{ accentColor: 'var(--color-evidence)' }}
              />
            </label>
            <label style={{ ...T_LABEL, display: 'flex', flexDirection: 'column', gap: 4 }}>
              minimum edge amount
              <input
                type="range"
                min={0}
                max={100}
                value={Math.min(100, Math.round((minAmount / 100_000_000) * 100))}
                onChange={(event) => publish({ min_minor: String(Math.round((Number(event.target.value) / 100) * 100_000_000)) })}
                style={{ accentColor: 'var(--color-evidence)' }}
              />
            </label>
            <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>
              {count(data.edges.length)} edges total · {count(data.nodes.length)} nodes returned · window{' '}
              {data.window.from.slice(0, 10)} → {data.window.to.slice(0, 10)}
            </p>
          </div>
        </Pane>

        {data.truncated ? (
          <Pane id="cap" title="Cap reached" operation="Reporting the subgraph cap" meta={graph.meta} skeleton={{ columns: [{ key: 'c', width: '100%' }], rows: 2 }}>
            <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', maxWidth: '40ch' }}>
              The server capped this subgraph at {count(data.cap)} nodes. {count(data.collapsed_communities.length)}{' '}
              communities are drawn as meta-nodes, each labelled with its true size:
            </p>
            <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0 }}>
              {data.collapsed_communities.map((entry) => (
                <li key={entry.community_id} style={{ display: 'flex', justifyContent: 'space-between', ...HAIRLINE_BOTTOM_ROW, padding: '3px 0' }}>
                  <span style={{ ...T_MONO, fontSize: 'var(--text-micro)', color: 'var(--color-ink)' }}>community {String(entry.community_id)}</span>
                  <span className="u-num" style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>{count(entry.true_size)} accounts</span>
                </li>
              ))}
            </ul>
          </Pane>
        ) : null}

        <Pane id="overlay-counts" title="What the overlays select" operation="Counting overlay members" meta={graph.meta} skeleton={{ columns: [{ key: 'k', width: '70%' }, { key: 'v', width: '30%', align: 'end' }], rows: 5 }}>
          <OverlayCounts subgraph={data} />
        </Pane>

        {selected !== null ? <NodeDetail node={findNode(visible?.nodes ?? [], selected)} edges={selectEdges(visible?.edges ?? [], selected)} /> : null}
      </div>
    </div>
  );
}

const HAIRLINE_BOTTOM_ROW = { borderBottom: '1px solid var(--color-hairline)' };

function findNode(nodes: readonly GraphNode[], key: string): GraphNode | null {
  return nodes.find((node) => node.key === key) ?? null;
}

function selectEdges(edges: readonly GraphEdge[], key: string): GraphEdge[] {
  return edges.filter((edge) => edge.source === key || edge.target === key);
}

function OverlayCounts({ subgraph }: { subgraph: Subgraph }): ReactElement {
  const rows = [
    { label: 'cycle members', value: subgraph.overlays.cycles },
    { label: 'high-velocity hops', value: subgraph.overlays.high_velocity_hops },
    { label: 'fan stars', value: subgraph.overlays.fan_stars },
    { label: 'communities present', value: subgraph.overlays.dense_communities },
    { label: 'flagged accounts', value: subgraph.overlays.flagged },
  ];
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
      {rows.map((row) => (
        <li key={row.label} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', ...HAIRLINE_BOTTOM_ROW }}>
          <span style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>{row.label}</span>
          <span className="u-num" style={{ ...T_LABEL, color: 'var(--color-ink)' }}>{count(row.value)}</span>
        </li>
      ))}
    </ul>
  );
}

function NodeDetail({ node, edges }: { node: GraphNode | null; edges: GraphEdge[] }): ReactElement {
  if (node === null) return <span />;
  const total = edges.reduce((sum, edge) => sum + edge.total.minor, 0);
  return (
    <Pane id="node" title="Selected node" operation="Reading the selected node" meta={null} skeleton={{ columns: [{ key: 'n', width: '100%' }], rows: 3 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <AccountChip accountKey={node.key} href={node.node_type === 'meta' ? `/network?account=${node.key}` : `/cases/${node.key}`} />
        <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
          {node.node_type} · community {String(node.community_id)} · degree {count(node.degree)} ·{' '}
          {node.hops === 0 ? 'the root' : `${String(node.hops)} hop${node.hops === 1 ? '' : 's'} out`}
        </p>
        {node.true_size !== null ? (
          <p style={{ ...T_LABEL, color: 'var(--color-ink)' }}>
            meta-node standing for {count(node.true_size)} accounts — the label is the true size, not the drawn one
          </p>
        ) : null}
        {node.node_type === 'rail' ? (
          <p style={{ ...T_MICRO, color: 'var(--color-state-running)' }}>
            typed as a rail by the supernode guard: excluded from fan-in and fan-out scoring, shown because it is part of
            the topology
          </p>
        ) : null}
        {node.band !== null ? <p style={{ ...T_LABEL }}>band <BandBadge band={node.band} describe={false} /></p> : null}
        {node.exposure !== null ? (
          <p style={{ ...T_LABEL, color: 'var(--color-ink)' }}>
            exposure {compactFromMinor(node.exposure.minor, node.exposure.decimals)} {node.exposure.currency}
          </p>
        ) : null}
        <p style={{ ...T_MICRO, color: 'var(--color-ink-faint)' }}>{count(edges.length)} drawn edges · {compactFromMinor(total, 2)} minor moved</p>
        {edges.some((edge) => edge.typology !== null) ? (
          <p style={{ ...T_MICRO, color: 'var(--color-ink-muted)' }}>
            typologies on these edges:{' '}
            {[...new Set(edges.map((edge) => edge.typology).filter((entry): entry is NonNullable<typeof entry> => entry !== null))].map((typology) => (
              <span key={typology} title={TYPOLOGY_META[typology].name}>
                {' '}
                <Icon name={glyphFor(typology)} size={12} /> {TYPOLOGY_META[typology].code}
              </span>
            ))}
          </p>
        ) : null}
      </div>
    </Pane>
  );
}

