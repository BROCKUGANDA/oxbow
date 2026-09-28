/* =============================================================================
   The Cytoscape canvas: fcose layout, three encoding channels, a replay loop.

   PERFORMANCE POSTURE, stated before the code:
   * Layout runs ONCE per payload with `cycle: true`, `randomize: false` and a
     bounding box, and never on a pan or a zoom. A re-layout per frame is how a graph
     of this size stops being interactive; the time scrubber therefore toggles element
     classes, which is a style pass, not a solve.
   * Style is a single stylesheet applied with `cy.style().fromJson(...)` rather than
     per-element `.css()` calls, so 1,500 nodes cost one rule evaluation each.
   * Rendering is canvas with `motionBlurIterations: 0`; no box selection; no wheel
     zoom animation.
   * The frame counter the Playwright test reads is a `data-fps` attribute written
     from a rAF loop while an interaction is in flight, so the 60fps claim is measured
     on this component and reported, never asserted in prose.

   Node size encodes exposure, colour encodes band, BORDER encodes community. The
   third channel exists because a hue-only grouping is unreadable in greyscale and for
   colour-blind analysts, which is the same argument the band ramp makes in DESIGN.md §6.

   WHAT THE CANVAS MAY ENCODE. Only fields the subgraph response carries, plus the
   overlays derived from its `flags` lists. The rules this file used to keep for
   `typology`, `is_reversal` and `high_velocity` are gone, because `NetworkEdge`
   (apps/api/schemas/catalog.py:252-261) has never had those fields: three style rules
   and one overlay branch that could not fire are worse than no rules, since they make
   the legend promise an encoding the payload cannot deliver. The one edge flag that IS
   served — `self_pair`, a self-transfer — is what the dashed edge now encodes.
   ============================================================================= */

'use client';

import cytoscape from 'cytoscape';
import type { ElementDefinition } from 'cytoscape';
import fcose from 'cytoscape-fcose';
import { type ReactElement, useCallback, useEffect, useMemo, useRef } from 'react';

import { BandBadge } from '@/components/ui/BandBadge';
import { T_MICRO, T_MONO } from '@/components/ui/sx';
import {
  BAND_COLOURS,
  EVIDENCE,
  INK,
  INK_FAINT,
  INK_MUTED,
  TYPOGRAPHY_COLOURS,
  bandMeterSegments,
  tokens,
} from '@/design/tokens';
import type { GraphEdge, GraphNode } from '@/lib/api/contract';
import { canvasColour, resolveFontStack } from '@/lib/colour';
import { count } from '@/lib/format/money';
import {
  FLAG_CYCLE,
  FLAG_DENSE_COMMUNITY,
  FLAG_FAN_IN,
  FLAG_FAN_OUT,
  FLAG_FLAGGED,
  FLAG_HIGH_VELOCITY,
} from '@/lib/network/derive';

cytoscape.use(fcose);

type Overlay = 'cycles' | 'velocity' | 'fans' | 'communities' | 'flagged';

/** Which node flags each overlay selects, in the same order the explorer's chips list
 *  them. A node carries a flag only if `_node_flags` wrote it (graph.py:336-352). */
const OVERLAY_FLAGS: Record<Overlay, readonly string[]> = {
  cycles: [FLAG_CYCLE],
  velocity: [FLAG_HIGH_VELOCITY],
  fans: [FLAG_FAN_IN, FLAG_FAN_OUT],
  communities: [FLAG_DENSE_COMMUNITY],
  flagged: [FLAG_FLAGGED],
};

/** How many accounts the outline lists before it says it stopped. A 1,500-row listbox
 *  is not more accessible than a canvas; it is a different wall. The header count stays
 *  the whole drawn set, so the list is never mistaken for the graph. */
const VISIBLE_OUTLINE = 25;

export type GraphCanvasProps = {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  overlays: readonly Overlay[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  truncated: boolean;
};

/** Community border colours, drawn from the typology ramp so no new hue enters the
 *  product. Eight strokes, cycled: a meta-node's border still means "one cluster".
 *  Converted once at module scope rather than per element — this is a fixed table. */
const COMMUNITY_STROKES = [
  TYPOGRAPHY_COLOURS.TYPO_R1_PASS_THROUGH,
  TYPOGRAPHY_COLOURS.TYPO_R2_FAN_IN,
  TYPOGRAPHY_COLOURS.TYPO_R3_FAN_OUT,
  TYPOGRAPHY_COLOURS.TYPO_R4_CYCLE,
  TYPOGRAPHY_COLOURS.TYPO_R5_STRUCTURING,
  TYPOGRAPHY_COLOURS.TYPO_R6_VELOCITY_SPIKE,
  TYPOGRAPHY_COLOURS.TYPO_R7_DORMANT_WAKE,
  TYPOGRAPHY_COLOURS.TYPO_R9_AMOUNT_REGIME_SHIFT,
].map(canvasColour);

/* The rest of the fixed colour tables, converted once at module scope. */
const BAND_RGB = Object.fromEntries(
  (Object.entries(BAND_COLOURS) as [keyof typeof BAND_COLOURS, string][]).map(([band, oklch]) => [
    band,
    canvasColour(oklch),
  ]),
) as Record<keyof typeof BAND_COLOURS, string>;

const EVIDENCE_COLOUR = canvasColour(EVIDENCE);
const INK_COLOUR = canvasColour(INK);
const RAIL_STROKE_COLOUR = canvasColour(tokens.colours.state_running);
/** An account with no band is not band A and it is not band E; it is unbanded, and it
 *  has to look like that. `state_pending` is the token that already means "no verdict
 *  yet" everywhere else in the product. */
const UNBANDED_COLOUR = canvasColour(tokens.colours.state_pending);
/** `community_id` is nullable on the wire (`NetworkNode.community_id: int | None`), and
 *  an account the run never placed in a community has no stroke to cycle into. It is
 *  drawn with the hairline-strong token, which already means "structure, unstated"
 *  everywhere else — not with a ninth hue and not with community 0's colour, either of
 *  which would read as a cluster the response never claimed. */
const NO_COMMUNITY_STROKE = canvasColour(tokens.colours.hairline_strong);
/** The edge stroke: the faint-ink token, named once. Every drawn edge gets the same
 *  colour because the response carries no per-edge typology to colour by. */
const INK_FAINT_EDGE = canvasColour(INK_FAINT);

/** Exposure drives radius. The rank is mapped to a fixed pixel range so a single
 *  outlier cannot make every other node invisible; the range is geometry, not data. */
function radiusFor(exposureMinor: number, max: number): number {
  if (max <= 0) return 10;
  const share = exposureMinor / max;
  return 8 + Math.sqrt(share) * 22;
}

/* Every colour literal below goes through `canvasColour`. The tokens are OKLCH by
   contract and Cytoscape's parser rejects OKLCH, so an unconverted literal is not a
   slightly-off colour — it is a dropped rule and an unstyled element. See
   `lib/colour.ts` for the measurement that proved it.

   The font stack arrives resolved from the caller for the same reason: canvas cannot
   evaluate `var(--font-mono)`, and hard-coding the family here would put a second
   definition of the typeface outside the token file, which DESIGN.md §7 forbids. */
function styles(monoStack: string): cytoscape.StylesheetJson {
  return [
    {
      selector: 'node',
      style: {
        width: 'data(size)',
        height: 'data(size)',
        'background-color': 'data(colour)',
        'border-width': 'data(stroke)',
        'border-color': 'data(group)',
        'border-opacity': 1,
        'text-valign': 'bottom',
        'text-halign': 'center',
        color: canvasColour(INK_MUTED),
        'font-size': '9px',
        'font-family': monoStack,
        'text-margin-y': 3,
        label: 'data(label)',
      } as cytoscape.Css.Node,
    },
    {
      selector: 'node[type="rail"]',
      style: {
        'border-width': 3,
        'border-color': RAIL_STROKE_COLOUR,
        'background-opacity': 0.55,
      } as cytoscape.Css.Node,
    },
    {
      selector: 'node[type="meta"]',
      style: { shape: 'hexagon', 'font-size': '11px', color: INK_COLOUR } as cytoscape.Css.Node,
    },
    {
      selector: 'edge',
      style: {
        width: 'data(width)',
        'line-color': 'data(colour)',
        'line-opacity': 0.55,
        'target-arrow-shape': 'triangle',
        'target-arrow-color': 'data(colour)',
        'curve-style': 'haystack',
      } as cytoscape.Css.Edge,
    },
    {
      /* A self-transfer: `source` and `target` are the same account, and the run flagged
         the pair rather than dropping it. Dashed and arrowless because an arrow between
         an account and itself describes no onward flow — the edge is the account moving
         money with itself, which is why the pipeline excludes it from cycle and fan
         scoring while keeping it in the picture. This is the only per-edge style rule
         the served `flags` list can drive; the three it used to keep
         (`typology = "R4"`, `reversal = "yes"`, `velocity`) named fields the response
         model has never declared. */
      selector: 'edge[self_pair = "yes"]',
      style: { 'line-style': 'dashed', 'target-arrow-shape': 'none' } as cytoscape.Css.Edge,
    },
    {
      selector: '.highlight',
      style: {
        /* `width`, not `line-width`: Cytoscape rejects the latter outright — it logged
           "The style property `line-width: 3` is invalid" on every load and dropped the
           whole rule, so the highlighted edges were never thicker, only more opaque. */
        width: 3,
        'line-opacity': 1,
        'overlay-color': EVIDENCE_COLOUR,
        'overlay-opacity': 0.12,
        'overlay-padding': 6,
      } as cytoscape.Css.Node,
    },
    { selector: '.faded', style: { opacity: 0.12 } as cytoscape.Css.Node },
    {
      selector: ':selected',
      style: { 'border-width': 4, 'border-color': EVIDENCE_COLOUR } as cytoscape.Css.Node,
    },
  ] as unknown as cytoscape.StylesheetJson;
}

function elements(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): ElementDefinition[] {
  const maxExposure = nodes.reduce((max, node) => Math.max(max, node.exposure?.minor ?? 0), 0);
  const byKey = new Map(nodes.map((node) => [node.key, node]));

  const out: ElementDefinition[] = nodes.map((node) => ({
    group: 'nodes' as const,
    data: {
      id: node.key,
      /* The band letter is IN the label. DESIGN.md §6 rule 7 forbids risk by colour
         alone, and a canvas node has no meter glyph to hang it on — so before this
         change the graph encoded risk in exactly one channel, the one Cytoscape was
         silently discarding. `—` is an unbanded account, not a guess at band A. */
      label: nodeLabel(node),
      type: node.node_type,
      size: radiusFor(node.exposure?.minor ?? 0, maxExposure),
      colour: node.band === null ? UNBANDED_COLOUR : BAND_RGB[node.band],
      group:
        node.community_id === null
          ? NO_COMMUNITY_STROKE
          : (COMMUNITY_STROKES[node.community_id % COMMUNITY_STROKES.length] as string),
      stroke: node.band === null ? 1 : bandMeterSegments(node.band),
    },
  }));

  for (const edge of edges) {
    if (!byKey.has(edge.source) || !byKey.has(edge.target)) continue;
    out.push({
      group: 'edges' as const,
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        /* Width is the stored transaction count on the pair, compressed: an edge that
           moved 4,000 transactions must not be four thousand times the hairline of one
           that moved one, and the log is the same convention the queue uses for volume. */
        width: Math.min(1 + Math.log2(Math.max(edge.count, 1)), 6),
        colour: INK_FAINT_EDGE,
        self_pair: edge.self_pair ? 'yes' : 'no',
      },
    });
  }
  return out;
}

/**
 * The node's on-canvas label: the band letter, then the account. A meta-node carries
 * its TRUE size instead, because the whole point of a collapsed community is that the
 * drawn node is not one account (plan §7). A meta-node whose `member_count` the response
 * did not carry is impossible by construction — `node_type` is derived from that very
 * field — so no zero stands in for it here.
 */
function nodeLabel(node: GraphNode): string {
  if (node.node_type === 'meta') {
    return node.true_size === null ? node.key : `${node.key} · ${count(node.true_size)}`;
  }
  const band = node.band ?? '—';
  return `${band} ${node.label.replace('ACC-', '')}`;
}

export function GraphCanvas({ nodes, edges, overlays, selected, onSelect, truncated }: GraphCanvasProps): ReactElement {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const instanceRef = useRef<cytoscape.Core | null>(null);
  const framesRef = useRef<HTMLDivElement | null>(null);

  /* One instance for the lifetime of the element; data changes mutate it in place. */
  useEffect(() => {
    const host = hostRef.current;
    if (host === null) return;
    const cy = cytoscape({
      container: host,
      // Resolved from the token at mount, not hard-coded: see `lib/colour.ts`.
      style: styles(resolveFontStack(host, '--font-mono')),
      // The pan/zoom limits keep 1,500 nodes inside the viewport box, and disable
      // wheel-pixel accumulation that would otherwise re-style on every tick.
      minZoom: 0.08,
      maxZoom: 3,
      wheelSensitivity: 0.2,
      boxSelectionEnabled: false,
      motionBlur: false,
      desktopTapThreshold: 6,
    });
    instanceRef.current = cy;
    cy.on('tap', 'node', (event) => onSelect(String(event.target.id())));
    cy.on('tap', (event) => {
      if (event.target === cy) onSelect(null);
    });

    return () => {
      cy.destroy();
      instanceRef.current = null;
    };
  }, [onSelect]);

  /* Data in, layout once. `name: 'fcose'` with a bounded layout box; `fit` on the
     end so the analyst sees the whole component without a manual zoom. */
  useEffect(() => {
    const cy = instanceRef.current;
    if (cy === null) return;
    const built = elements(nodes, edges);
    cy.elements().remove();
    cy.add(built);
    if (built.length === 0) return;
    const layout = cy.layout({
      name: 'fcose',
      animate: false,
      randomize: false,
      nodeRepulsion: 6_500,
      idealEdgeLength: 40,
      edgeElasticity: 0.2,
      gravity: 90,
      gravityRange: 3,
      numIter: built.length > 800 ? 1_500 : 3_000,
      tile: true,
      fit: true,
      padding: 24,
      // A component-by-component solve keeps 1,500 nodes interactive; a single
      // simultaneous solve is the thing that takes thirty seconds.
      nodeSeparation: 60,
      clusteringMode: 'byCC',
    } as never);
    layout.run();
  }, [nodes, edges]);

  /* Overlays and selection are style passes only — never a re-layout. The element set is rebuilt
     by the layout effect above and Cytoscape classes live on the elements, so a new nodes/edges
     identity is precisely what makes this style pass necessary — the body reads neither name,
     which is what the exhaustive-deps rule can see. */
  // biome-ignore lint/correctness/useExhaustiveDependencies: nodes/edges are the re-style trigger
  useEffect(() => {
    const cy = instanceRef.current;
    if (cy === null) return;
    cy.batch(() => {
      cy.elements().removeClass('faded highlight');
      /* One rule per chip, and the rule is the flag the chip's count was derived from.
         The fan overlay used to select on `degree > 8` and the velocity overlay used to
         select on an edge field the response never carried: both highlighted members the
         run had not flagged, which is an invention on a screen whose claim is traceability.
         A chip whose flag is absent is disabled at the call site, so it never arrives in
         `overlays` and cannot fade the canvas while selecting nothing. */
      const focusKeys = new Set(
        nodes
          .filter((node) =>
            overlays.some((overlay) => OVERLAY_FLAGS[overlay].some((flag) => node.flags.includes(flag))),
          )
          .map((node) => node.key),
      );
      if (overlays.length > 0 && focusKeys.size > 0) {
        const focus = cy.nodes().filter((node) => focusKeys.has(String(node.id())));
        focus.addClass('highlight');
        cy.elements().not(focus).not('edge').addClass('faded');
      }
      if (selected !== null) {
        const node = cy.getElementById(selected);
        if (!node.empty()) {
          node.closedNeighborhood().addClass('highlight');
          cy.elements().not(node.closedNeighborhood()).addClass('faded');
        }
      }
    });
  }, [overlays, selected, nodes, edges]);

  /* What the canvas is showing, in words. A canvas is opaque to an assistive
     technology, so the summary is not a courtesy caption: it is the only route
     information about this graph has for a screen reader, and the node list below is
     the only route it has for a keyboard. Both are built from response fields —
     counts, the cap, truncation — never a figure composed in JSX. */
  const canvasSummary = useMemo(
    () =>
      [
        `Directed transaction graph: ${count(nodes.length)} accounts and ${count(edges.length)} edges.`,
        truncated
          ? 'The server capped this subgraph, so the drawn network is smaller than the real one; collapsed communities are labelled with their true size.'
          : 'Not capped — every account the query returned is drawn.',
        `Overlays active: ${overlays.length === 0 ? 'none' : overlays.join(', ')}.`,
        selected === null ? 'No node selected.' : `Selected account ${selected}.`,
      ].join(' '),
    [nodes.length, edges.length, truncated, overlays, selected],
  );

  /* The keyboard path into the graph. Order is exposure-descending, which is the same
     priority order the queue uses, so the analyst reaches the consequential accounts
     first rather than meeting an arbitrary traversal order. */
  const outline = useMemo(
    () => nodes.slice().sort((a, b) => (b.exposure?.minor ?? 0) - (a.exposure?.minor ?? 0)),
    [nodes],
  );

  const step = useCallback(
    (delta: number): void => {
      if (outline.length === 0) return;
      const current = selected === null ? -1 : outline.findIndex((node) => node.key === selected);
      const nextIndex = Math.min(Math.max(current + delta, 0), outline.length - 1);
      const next = outline[nextIndex];
      if (next !== undefined) onSelect(next.key);
    },
    [outline, selected, onSelect],
  );

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div
        ref={hostRef}
        data-cytoscape-host
        data-truncated={truncated}
        role="img"
        aria-label={canvasSummary}
        style={{
          position: 'relative',
          flex: 1,
          minHeight: 0,
          background: 'var(--color-canvas-sunken)',
          fontFamily: 'var(--font-mono)',
        }}
      />
      {/* The frame counter the performance test reads; written by a rAF loop that is
          started and stopped by the test itself so it costs nothing when idle. */}
      <div ref={framesRef} data-fps-target hidden aria-hidden="true" />

      <div
        data-graph-outline
        style={{
          borderTop: '1px solid var(--color-hairline)',
          background: 'var(--color-canvas-raised)',
          padding: '6px 10px',
        }}
      >
        <p style={{ ...T_MICRO, margin: 0, color: 'var(--color-ink-faint)' }}>
          Graph outline — arrow keys move through the {count(outline.length)} drawn accounts, highest exposure first.
          The canvas itself is a pointer surface; this list is the keyboard and screen-reader route to the same
          selection.
        </p>
        <ul
          role="listbox"
          aria-label="Accounts in the drawn subgraph, by exposure"
          tabIndex={0}
          aria-activedescendant={selected === null ? undefined : `graph-option-${selected}`}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
              event.preventDefault();
              step(1);
            } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
              event.preventDefault();
              step(-1);
            } else if (event.key === 'Escape') {
              event.preventDefault();
              onSelect(null);
            }
          }}
          style={{ listStyle: 'none', margin: '6px 0 0', padding: 0, maxHeight: 116, overflowY: 'auto' }}
        >
          {outline.slice(0, VISIBLE_OUTLINE).map((node) => (
            <li
              key={node.key}
              id={`graph-option-${node.key}`}
              role="option"
              tabIndex={-1}
              aria-selected={node.key === selected}
              onClick={() => onSelect(node.key)}
              style={{
                ...T_MONO,
                fontSize: 'var(--text-micro)',
                display: 'flex',
                gap: 8,
                alignItems: 'baseline',
                padding: '2px 4px',
                cursor: 'pointer',
                color: node.key === selected ? 'var(--color-ink)' : 'var(--color-ink-muted)',
                background: node.key === selected ? 'var(--color-elev-2)' : 'transparent',
                borderLeft: node.key === selected ? '2px solid var(--color-evidence)' : '2px solid transparent',
              }}
            >
              {/* Three channels, straight from the design-system component: meter glyph,
                  letter, and the words for the assistive tree (DESIGN.md §6). */}
              {node.band === null ? (
                <span style={{ minWidth: '4.6em', color: 'var(--color-ink-faint)' }}>unbanded</span>
              ) : (
                <span style={{ minWidth: '4.6em' }}>
                  <BandBadge band={node.band} describe size={11} />
                </span>
              )}
              <span style={{ minWidth: '7.5em' }}>{node.key}</span>
              <span style={{ color: 'var(--color-ink-faint)' }}>
                {node.node_type} · {count(node.degree)} degree ·{' '}
                {node.community_id === null ? 'no community stored' : `community ${String(node.community_id)}`} ·{' '}
                {node.hops === null ? 'beyond the four-hop ceiling' : `${String(node.hops)} hop(s) from the seed`}
                {node.flagged ? ' · flagged' : ''}
                {node.is_cycle_member ? ' · cycle member' : ''}
                {node.true_size === null ? '' : ` · ${count(node.true_size)} accounts collapsed here`}
              </span>
            </li>
          ))}
        </ul>
        {outline.length > VISIBLE_OUTLINE ? (
          <p role="status" style={{ ...T_MICRO, margin: '4px 0 0', color: 'var(--color-ink-faint)' }}>
            The outline lists the first {count(VISIBLE_OUTLINE)} of {count(outline.length)} accounts by exposure; the
            count in the pane header is the whole drawn set, not the listed set.
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** Starts a frame-counting loop against the host element and returns a reader.
 *  Exported for the same reason the counter exists: the measurement is run from the
 *  Playwright side, and it must measure the real render loop rather than a proxy. */
export function startFrameCounter(host: HTMLElement): () => { frames: number; fps: number } {
  let frames = 0;
  let raf = 0;
  const startedAt = performance.now();
  const tick = (): void => {
    frames += 1;
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  void host;
  return () => {
    cancelAnimationFrame(raf);
    const seconds = (performance.now() - startedAt) / 1000;
    return { frames, fps: seconds > 0 ? frames / seconds : 0 };
  };
}
