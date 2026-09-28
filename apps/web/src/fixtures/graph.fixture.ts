/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   The subgraph. Two sizes: the 2-hop demo neighbourhood, and the 1,500-node
   stress payload the frame-rate measurement runs against. The stress payload is
   generated from a fixed seed so the measured number is reproducible rather than
   a lucky run — the same discipline plan §10 asks of a model result.

   THESE ARE THE SERVED BYTES, NOT THE SCREEN'S CONCEPTS. `GET /api/graph/subgraph`
   answers `NetworkSubgraph` (`apps/api/schemas/catalog.py`), and the explorer's hop
   distances, time buckets and overlay counts are derived from it in
   `lib/api/contract.ts:deriveSubgraph`. The double therefore emits the served shape
   and lets the same decoder run over it: a fixture that pre-derived those three
   numbers could agree with the client while disagreeing with the server, which is the
   one thing this file exists to make impossible. */

import type {
  ServedCommunityMetaNode,
  ServedNetworkEdge,
  ServedNetworkNode,
  ServedNetworkSubgraph,
} from '../lib/api/contract';
import { FLAG_DENSE_COMMUNITY, FLAG_FLAGGED, FLAG_SELF_PAIR, hopDepths } from '../lib/network/derive';
import { BAND_LETTERS, money, seeded } from './common.fixture';

const START = Date.UTC(2026, 8, 1, 0, 0, 0);
const DAY_MS = 86_400_000;
const NODE_CAP = 1_500;
/** `window_start` / `window_end`, served as instants and nullable on the wire. */
const WINDOW = { from: new Date(START).toISOString(), to: new Date(START + 28 * DAY_MS).toISOString() };

/** A node as `NetworkNode` declares it: `id` is an account key, and `flags` is the list
 *  `_node_flags` wrote. `cycle`, `fan_in`, `fan_out` and `high_velocity_hops` are absent
 *  from every list here because the pipeline never writes them (CONTRACT-GAPS.md) — the
 *  double does not supply what the server withholds, which is the whole reason the
 *  explorer now names the overlays a run cannot back. */
function node(overrides: Partial<ServedNetworkNode> & { id: string }): ServedNetworkNode {
  const band = overrides.band === undefined ? null : overrides.band;
  return {
    id: overrides.id,
    label: overrides.label ?? overrides.id,
    band,
    exposure: overrides.exposure ?? money(8_000_000),
    degree: overrides.degree ?? 2,
    community_id: overrides.community_id ?? null,
    is_seed: overrides.is_seed ?? false,
    is_rail: overrides.is_rail ?? false,
    // `_node_flags` marks a D or E band as `flagged` and carries `dense_community` from
    // the stored community density; nothing else.
    flags:
      overrides.flags ??
      (band === 'D' || band === 'E' ? [FLAG_FLAGGED] : band === 'A' || band === 'B' ? [FLAG_DENSE_COMMUNITY] : []),
  };
}

/** An edge as `NetworkEdge` declares it. There is no edge id on the wire and no typology:
 *  `(run_id, src, dst)` is unique per `uq_graph_edge`, and the only flag the pipeline
 *  writes is `self_pair`. */
function edge(overrides: {
  source: string;
  target: string;
  first_ts: string;
  txn_count: number;
  total: ServedNetworkEdge['total'];
}): ServedNetworkEdge {
  return {
    source: overrides.source,
    target: overrides.target,
    total: overrides.total,
    txn_count: overrides.txn_count,
    first_ts: overrides.first_ts,
    last_ts: new Date(Date.parse(overrides.first_ts) + 3_600_000).toISOString(),
    flags: overrides.source === overrides.target ? [FLAG_SELF_PAIR] : [],
  };
}

function frame(served: {
  seed: string;
  hops: number;
  nodes: ServedNetworkNode[];
  edges: ServedNetworkEdge[];
  collapsed?: ServedCommunityMetaNode[];
  truncated?: boolean;
  truncationReason?: string | null;
}): ServedNetworkSubgraph {
  return {
    run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
    seed_account_key: served.seed,
    hops: served.hops,
    nodes: served.nodes,
    edges: served.edges,
    collapsed_communities: served.collapsed ?? [],
    node_cap: NODE_CAP,
    truncated: served.truncated ?? false,
    truncation_reason: served.truncationReason ?? null,
    window_start: WINDOW.from,
    window_end: WINDOW.to,
    counterparty_note: null,
  };
}

function neighbourhood(): ServedNetworkSubgraph {
  const next = seeded(2024);
  const nodes: ServedNetworkNode[] = [];
  const edges: ServedNetworkEdge[] = [];
  const communities = 4;
  const seed = 'ACC-7F2A19';

  for (let index = 0; index < 68; index += 1) {
    const community = index % communities;
    const band = index % 11 === 0 ? null : (BAND_LETTERS[index % BAND_LETTERS.length] ?? 'A');
    nodes.push(
      node({
        id: index === 0 ? seed : `ACC-${(0x300000 + index * 0x539).toString(16).toUpperCase().slice(0, 6)}`,
        band,
        exposure: money(Math.round(8_000_000 + next() * 400_000_000)),
        degree: 2 + Math.floor(next() * 14),
        community_id: community,
        is_seed: index === 0,
        is_rail: index === 40,
      }),
    );
  }

  for (let index = 0; index < 96; index += 1) {
    const source = nodes[index % nodes.length];
    const target = nodes[(index * 7 + 3) % nodes.length];
    if (source === undefined || target === undefined) continue;
    edges.push(
      edge({
        source: source.id,
        target: target.id,
        first_ts: new Date(START + Math.floor(next() * 20) * DAY_MS + index * 60_000).toISOString(),
        txn_count: 1 + Math.floor(next() * 12),
        total: money(Math.round(3_000_000 + next() * 90_000_000)),
      }),
    );
  }
  // One self-transfer, so the only edge flag the pipeline really writes is reachable from
  // a live route rather than only from the gallery: `self_pair` on `A -> A`.
  const first = nodes[0];
  if (first !== undefined) {
    edges.push(
      edge({
        source: first.id,
        target: first.id,
        first_ts: new Date(START + DAY_MS).toISOString(),
        txn_count: 3,
        total: money(12_000_000),
      }),
    );
  }

  return frame({ seed, hops: 2, nodes, edges });
}

const demo = neighbourhood();

export const subgraph: ServedNetworkSubgraph = demo;

/* ------------------------------------------------------- traversal slicing -- */

/**
 * The subgraph a `(root, hops)` query returns, sliced from the demo neighbourhood.
 *
 * The real route traverses; the double has to, or every control on the explorer that
 * publishes `hops` or `account` into the query string is a dead slider in dev and the
 * states behind it are unreachable. Distances come from `hopDepths` — the client's mirror
 * of `routers/graph.py:_hop_depths`, the same BFS the server runs over the same edges — so
 * the slice is produced by the same computation the decoded payload will be checked
 * against, not by a second copy of it written here. An unknown account answers with an
 * empty subgraph, which is what a graph with no such vertex returns, not an error.
 */
export function traversedSubgraph(account: string, hops: number): ServedNetworkSubgraph {
  if (account === '') return { ...demo, hops };

  const start = demo.nodes.find((entry) => entry.id === account);
  if (start === undefined) {
    return frame({ seed: account, hops, nodes: [], edges: [] });
  }

  const depths = hopDepths(account, demo.edges, hops);
  const reachable = new Set(depths.keys());
  const nodes = demo.nodes.filter((entry) => reachable.has(entry.id));
  const edges = demo.edges.filter((entry) => reachable.has(entry.source) && reachable.has(entry.target));

  return frame({ seed: account, hops, nodes, edges });
}

/** The 1,500-node cap variant, with two community meta-nodes carrying their true size. */
export function stressSubgraph(target = NODE_CAP): ServedNetworkSubgraph {
  const next = seeded(7717);
  const nodes: ServedNetworkNode[] = [];
  for (let index = 0; index < target; index += 1) {
    nodes.push(
      node({
        id: `N-${index.toString(36).toUpperCase()}`,
        band: BAND_LETTERS[index % BAND_LETTERS.length] ?? 'A',
        exposure: money(Math.round(1_000_000 + next() * 50_000_000)),
        degree: 2 + Math.floor(next() * 6),
        community_id: index % 24,
        is_seed: index === 0,
        is_rail: index % 97 === 0,
      }),
    );
  }
  const edges: ServedNetworkEdge[] = [];
  for (let index = 0; index < target * 2; index += 1) {
    const source = nodes[index % target];
    const other = nodes[(index * 13 + 5) % target];
    if (source === undefined || other === undefined) continue;
    edges.push(
      edge({
        source: source.id,
        target: other.id,
        first_ts: new Date(START + Math.floor(next() * 28) * DAY_MS).toISOString(),
        txn_count: 1,
        total: money(2_000_000),
      }),
    );
  }

  /* A collapsed community is announced by its representative account key plus its true
     member count — the shape `CommunityMetaNode` declares, and the only way a meta-node
     can be identified from the response rather than from a layout accident. The
     representative is a real node in the list, so `deriveSubgraph` can type it as `meta`
     and label it with the size the response states. */
  const collapsed: ServedCommunityMetaNode[] = [24, 25].map((communityId, index) => {
    const representative = nodes[index * 40 + 7];
    return {
      community_id: communityId,
      member_count: communityId === 24 ? 812 : 1_204,
      // An unpriced community is served as null, not as zero.
      total: index === 0 ? money(100_000_000) : null,
      representative_account_key: representative?.id ?? `N-${String(communityId)}`,
    };
  });
  const metaNodes: ServedNetworkNode[] = collapsed.map((entry) =>
    node({
      id: entry.representative_account_key,
      label: `community ${String(entry.community_id)}`,
      band: null,
      exposure: entry.total,
      degree: entry.member_count,
      community_id: entry.community_id,
      flags: [],
    }),
  );
  const attached = metaNodes.map((entry, index) =>
    edge({
      source: entry.id,
      target: nodes[index]?.id ?? entry.id,
      first_ts: WINDOW.from,
      txn_count: entry.degree,
      total: money(100_000_000),
    }),
  );

  return frame({
    seed: nodes[0]?.id ?? 'N-0',
    hops: 2,
    nodes: [...nodes, ...metaNodes],
    edges: [...edges, ...attached],
    collapsed,
    truncated: true,
    truncationReason: `the subgraph exceeds the ${String(NODE_CAP)}-node ceiling, so communities beyond it are drawn as meta-nodes`,
  });
}
