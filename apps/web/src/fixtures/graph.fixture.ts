/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The subgraph. Two sizes: the 2-hop demo neighbourhood, and the 1,500-node
   stress payload the frame-rate measurement runs against. The stress payload is
   generated from a fixed seed so the measured number is reproducible rather than
   a lucky run — the same discipline plan §10 asks of a model result. */

import type { GraphEdge, GraphNode, Subgraph } from '../lib/api/contract';
import { BAND_LETTERS, RULE_IDS, money, pick, seeded } from './common.fixture';

const START = Date.UTC(2026, 8, 1, 0, 0, 0);
const DAY_MS = 86_400_000;

function neighbourhood(): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const next = seeded(2024);
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const communities = 4;

  for (let index = 0; index < 68; index += 1) {
    const community = index % communities;
    const band = BAND_LETTERS[index % BAND_LETTERS.length];
    nodes.push({
      key: index === 0 ? 'ACC-7F2A19' : `ACC-${(0x300000 + index * 0x539).toString(16).toUpperCase().slice(0, 6)}`,
      band: index % 11 === 0 ? null : (band ?? 'A'),
      exposure: money(Math.round(8_000_000 + next() * 400_000_000)),
      degree: 2 + Math.floor(next() * 14),
      community_id: community,
      node_type: index === 40 ? 'rail' : index % 17 === 0 ? 'external' : 'account',
      flagged: index < 9,
      is_cycle_member: index < 12,
      hops: index < 8 ? 1 : 2,
      true_size: null,
    });
  }

  for (let index = 0; index < 96; index += 1) {
    const source = nodes[index % nodes.length];
    const target = nodes[(index * 7 + 3) % nodes.length];
    if (source === undefined || target === undefined) continue;
    const first = new Date(START + Math.floor(next() * 20) * DAY_MS + index * 60_000).toISOString();
    edges.push({
      id: `e-${String(index)}`,
      source: source.key,
      target: target.key,
      ts_first: first,
      ts_last: new Date(Date.parse(first) + Math.floor(next() * 6) * 3_600_000).toISOString(),
      count: 1 + Math.floor(next() * 12),
      total: money(Math.round(3_000_000 + next() * 90_000_000)),
      typology: index % 5 === 0 ? pick(RULE_IDS, next) : null,
      is_reversal: index === 61,
      high_velocity: index % 13 === 0,
    });
  }

  return { nodes, edges };
}

function stress(target: number): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const next = seeded(7717);
  const nodes: GraphNode[] = [];
  for (let index = 0; index < target; index += 1) {
    nodes.push({
      key: `N-${index.toString(36).toUpperCase()}`,
      band: BAND_LETTERS[index % BAND_LETTERS.length] ?? 'A',
      exposure: money(Math.round(1_000_000 + next() * 50_000_000)),
      degree: 2 + Math.floor(next() * 6),
      community_id: index % 24,
      node_type: index % 97 === 0 ? 'rail' : 'account',
      flagged: index % 11 === 0,
      is_cycle_member: index % 7 === 0,
      hops: index % 3,
      true_size: null,
    });
  }
  const edges: GraphEdge[] = [];
  for (let index = 0; index < target * 2; index += 1) {
    const source = nodes[index % target];
    const targetNode = nodes[(index * 13 + 5) % target];
    if (source === undefined || targetNode === undefined) continue;
    const first = new Date(START + Math.floor(next() * 28) * DAY_MS).toISOString();
    edges.push({
      id: `s-${String(index)}`,
      source: source.key,
      target: targetNode.key,
      ts_first: first,
      ts_last: new Date(Date.parse(first) + 3_600_000).toISOString(),
      count: 1,
      total: money(2_000_000),
      typology: null,
      is_reversal: false,
      high_velocity: index % 19 === 0,
    });
  }
  return { nodes, edges };
}

function buckets(edges: readonly GraphEdge[]): { bucket: string; edges: number }[] {
  const byDay = new Map<string, number>();
  for (const edge of edges) {
    const day = edge.ts_first.slice(0, 10);
    byDay.set(day, (byDay.get(day) ?? 0) + 1);
  }
  return [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([day, count]) => ({ bucket: `${day}T00:00:00Z`, edges: count }));
}

function overlays(edges: readonly GraphEdge[], nodes: readonly GraphNode[]): Subgraph['overlays'] {
  return {
    cycles: nodes.filter((node) => node.is_cycle_member).length,
    high_velocity_hops: edges.filter((edge) => edge.high_velocity).length,
    fan_stars: new Set(edges.map((edge) => edge.target)).size,
    dense_communities: new Set(nodes.map((node) => node.community_id)).size,
    flagged: nodes.filter((node) => node.flagged).length,
  };
}

const demo = neighbourhood();

export const subgraph: Subgraph = {
  nodes: demo.nodes,
  edges: demo.edges,
  truncated: false,
  cap: 1_500,
  collapsed_communities: [],
  window: { from: new Date(START).toISOString(), to: new Date(START + 28 * DAY_MS).toISOString() },
  hops: 2,
  edges_by_bucket: buckets(demo.edges),
  overlays: overlays(demo.edges, demo.nodes),
};

/* ------------------------------------------------------- traversal slicing -- */

/**
 * The subgraph a `(root, hops)` query returns, sliced from the demo neighbourhood.
 *
 * The real route traverses; the double has to, or every control on the explorer that
 * publishes `hops` or `account` into the query string is a dead slider in dev and the
 * states behind it are unreachable. Distances are recomputed from the root, edges are
 * the ones actually walked, and an unknown account answers with an empty subgraph —
 * which is what a graph with no such vertex returns, not an error.
 */
export function traversedSubgraph(account: string, hops: number): Subgraph {
  // Named rather than `window`: this module runs in the browser too, and shadowing that
  // global inside a fixture is a trap for the next person to edit it.
  const drawnWindow = { from: new Date(START).toISOString(), to: new Date(START + 28 * DAY_MS).toISOString() };

  // No root named is the whole demo neighbourhood, exactly as the default route draws it.
  if (account === '') return { ...subgraph, hops };

  const start = demo.nodes.find((node) => node.key === account);
  if (start === undefined) {
    return {
      nodes: [],
      edges: [],
      truncated: false,
      cap: 1_500,
      collapsed_communities: [],
      window: drawnWindow,
      hops,
      edges_by_bucket: [],
      overlays: { cycles: 0, high_velocity_hops: 0, fan_stars: 0, dense_communities: 0, flagged: 0 },
    };
  }

  const depth = new Map<string, number>([[start.key, 0]]);
  const walked = new Set<string>();
  for (let hop = 1; hop <= hops; hop += 1) {
    for (const edge of demo.edges) {
      if (edge.source === edge.target) continue;
      const from = depth.get(edge.source);
      if (from === undefined || from !== hop - 1 || walked.has(edge.id)) continue;
      walked.add(edge.id);
      if (!depth.has(edge.target)) depth.set(edge.target, hop);
    }
  }

  const edges = demo.edges.filter((edge) => walked.has(edge.id));
  /* Cycle membership is a property of the RUN, published per node: the detector worked
     over the whole window, not over this ball. It is carried through the slice
     unaltered, so `overlays.cycles` below counts the members this query actually
     returned rather than a number invented for the hop budget. */
  const nodes = demo.nodes
    .filter((node) => depth.has(node.key))
    .map((node) => ({ ...node, hops: depth.get(node.key) ?? 0 }));

  return {
    nodes,
    edges,
    truncated: false,
    cap: 1_500,
    collapsed_communities: [],
    window: drawnWindow,
    hops,
    edges_by_bucket: buckets(edges),
    overlays: overlays(edges, nodes),
  };
}


/** The 1,500-node cap variant, with two community meta-nodes carrying their true size. */
export function stressSubgraph(target = 1_500): Subgraph {
  const built = stress(target);
  const collapsed = [
    { community_id: 24, true_size: 812 },
    { community_id: 25, true_size: 1_204 },
  ];
  const metaNodes: GraphNode[] = collapsed.map((entry) => ({
    key: `META-${String(entry.community_id)}`,
    band: null,
    exposure: null,
    degree: entry.true_size,
    community_id: entry.community_id,
    node_type: 'meta',
    flagged: false,
    is_cycle_member: false,
    hops: 1,
    true_size: entry.true_size,
  }));
  const edges = [...built.edges, ...metaNodes.map((node, index) => ({
    id: `meta-${String(index)}`,
    source: node.key,
    target: built.nodes[index]?.key ?? node.key,
    ts_first: new Date(START).toISOString(),
    ts_last: new Date(START + DAY_MS).toISOString(),
    count: node.degree,
    total: money(100_000_000),
    typology: null,
    is_reversal: false,
    high_velocity: false,
  }))];
  return {
    nodes: [...built.nodes, ...metaNodes],
    edges,
    truncated: true,
    cap: target,
    collapsed_communities: collapsed,
    window: { from: new Date(START).toISOString(), to: new Date(START + 28 * DAY_MS).toISOString() },
    hops: 2,
    edges_by_bucket: buckets(edges),
    overlays: overlays(edges, built.nodes),
  };
}
