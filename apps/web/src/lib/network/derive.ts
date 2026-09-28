/* =============================================================================
   Network explorer derivations — the three numbers the UI shows that
   `GET /api/graph/subgraph` does not send.

   WHY THESE LIVE HERE AND NOT IN A COMPONENT. The route serves
   `NetworkSubgraph` (`apps/api/schemas/catalog.py:283-302`): nodes with
   `id / label / band / exposure / degree / community_id / is_seed / is_rail / flags`,
   edges with `source / target / total / txn_count / first_ts / last_ts / flags`,
   `collapsed_communities`, `node_cap`, `truncated`, `window_start / window_end`.
   The canvas needs three more things — hop distance per node, an edge-formation
   bucket per day for the scrubber, and an overlay count per flag — and the server
   computes none of them into the response. Each of the three is therefore derived
   here, as a pure function over the served payload, and unit tested. Nothing takes
   a default, invents a date, or fills an absent flag with zero and calls it a
   measurement: plan §14's rule is that a route may not render a value that is not
   in the response, and a derivation is only lawful when it is the *same* computation
   the server already ran on the *same* bytes.

   1. `hopDepths` mirrors :func:`api.routers.graph._hop_depths` (graph.py:385-404):
      an undirected breadth-first walk from the seed over the edge list, assigning the
      hop at which each key is first reached, with the route's own `MAX_HOPS = 4`
      (graph.py:53) as the ceiling. The server does not emit the map because the
      response model has no field for it, so this recomputation over the served edges
      is the same BFS over the same edge set and lands on the same depth for every
      node the route named. The one difference is deliberate and visible: where the
      server's dict simply omits a key it never reached (and `_collapse` later reads
      that as `MAX_HOPS + 1`), this returns `null` so the UI has to say "no path in
      the traversal" rather than print a sentinel as if it were a distance.

   2. `bucketEdges` is the scrubber's frame list. `edges_by_bucket` is a client-side
      name that the API has never served; the buckets are built from the served
      `first_ts` values, on the UTC calendar, and each frame's cutoff is a served
      instant — the latest `first_ts` inside that day — never a synthesised midnight.

   3. `overlayCounts` counts the nodes that actually carry each flag in this payload. Node flags are what `_node_flags` (graph.py:336-352) wrote: the
      flags of the edges touching the node, plus `dense_community` from the stored
      community density and `flagged` from a D/E band. Edge flags come out of the
      pipeline as `["self_pair"]` or nothing at all
      (`packages/pipeline/oxbow/adapters/warehouse/landing.py`, the `flags` key of the
      edges frame), so `cycle`, `high_velocity_hops`, `fan_in` and `fan_out` are names
      the literal `NetworkFlag` allows and this run may never have written. A count of
      zero is therefore "nothing in this payload carries the flag", which the explorer
      renders as a disabled chip naming that, not as a live toggle that selects nothing.
   ============================================================================= */

/** The route's own hop ceiling — `MAX_HOPS: Final = 4` in apps/api/routers/graph.py:53,
 *  enforced by the `le=MAX_HOPS` query validation at :96. Mirrored, not chosen. */
export const SERVER_HOP_CEILING = 4;

/** An edge as the traversal needs it: its two endpoints. Served `source` and `target`. */
export type TraversalEdge = { source: string; target: string };

/** A node as the overlay count needs it: its served `flags` list. */
export type FlagCarrier = { flags: readonly string[] };

/** A served edge timestamp, for the scrubber. */
export type DatedEdge = { ts_first: string };

/** One scrubber frame: a UTC calendar day, the latest served `first_ts` inside it, and
 *  how many edges were first seen in that day. */
export type EdgeBucket = {
  /** `YYYY-MM-DD`, the UTC day the bucket groups on. Stated as UTC because the client
   *  owns no timezone: `deployment_timezone` belongs to the window labels, not here. */
  utcDay: string;
  /** A served instant: the latest `first_ts` in this bucket. The scrubber's cutoff. */
  cutoff: string;
  edges: number;
};

/** The five overlay chips' counts, each a count of payload members carrying the flag. */
export type OverlayCounts = {
  cycles: number;
  high_velocity_hops: number;
  fan_stars: number;
  dense_communities: number;
  flagged: number;
};

/** Flags the explorer's chips are built on. `cycle` / `high_velocity_hops` / `fan_in` /
 *  `fan_out` / `dense_community` are the `NetworkFlag` literal (catalog.py:281);
 *  `flagged` is added by `_node_flags` for a D or E band and is not in that literal. */
export const FLAG_CYCLE = 'cycle';
export const FLAG_HIGH_VELOCITY = 'high_velocity_hops';
export const FLAG_FAN_IN = 'fan_in';
export const FLAG_FAN_OUT = 'fan_out';
export const FLAG_DENSE_COMMUNITY = 'dense_community';
export const FLAG_FLAGGED = 'flagged';
/** The one edge flag the pipeline writes today: a self-transfer, `A -> A`
 *  (`is_self_pair`, off `DERIVED_SELF_COLUMN = "is_self_transfer"`). */
export const FLAG_SELF_PAIR = 'self_pair';

/**
 * Shortest stored-edge distance from the seed, mirroring `_hop_depths`.
 *
 * Undirected, exactly like the server: the adjacency built there adds `dst` under `src`
 * *and* `src` under `dst`, so a payment in one direction is a link in both for the
 * purpose of the traversal. `null` means "no path from the seed within the route's
 * four-hop ceiling" — the same fact the server encodes by omitting the key.
 */
export function hopDepths(
  seed: string,
  edges: readonly TraversalEdge[],
  ceiling = SERVER_HOP_CEILING,
): Map<string, number | null> {
  const adjacency = new Map<string, Set<string>>();
  const link = (from: string, to: string): void => {
    const neighbours = adjacency.get(from);
    if (neighbours === undefined) adjacency.set(from, new Set([to]));
    else neighbours.add(to);
  };
  for (const edge of edges) {
    // The server sorts each neighbour list before expanding it (`_hop_depths`'s
    // `sorted(adjacency.get(node, ()))`). Sorting decides which node is *seen* first
    // inside one hop, not how far it is, so the depth values match either way; sorted
    // here too so a re-run of this function on the same payload is byte-stable.
    link(edge.source, edge.target);
    link(edge.target, edge.source);
  }

  const depths = new Map<string, number | null>([[seed, 0]]);
  let frontier: string[] = [seed];
  for (let hop = 1; hop <= ceiling; hop += 1) {
    const next: string[] = [];
    for (const node of frontier) {
      const neighbours = adjacency.get(node);
      if (neighbours === undefined) continue;
      for (const other of [...neighbours].sort()) {
        if (depths.has(other)) continue;
        depths.set(other, hop);
        next.push(other);
      }
    }
    frontier = next.sort();
  }
  return depths;
}

/** `null` for any endpoint the walk never reached, so the caller cannot mistake the
 *  absence of a distance for a distance of zero. */
export function depthOf(depths: ReadonlyMap<string, number | null>, key: string): number | null {
  return depths.get(key) ?? null;
}

/**
 * The scrubber's frames: served `first_ts` values grouped on the UTC calendar day,
 * oldest first, each carrying the latest served instant in it as the frame's cutoff.
 *
 * Edges with no `first_ts` are not bucketed into a fabricated day — they are left out
 * and the count of them is returned beside the list, so the UI can say how many edges
 * the replay does not order. The server declares `first_ts: datetime` (non-null), so
 * the empty list is a guard rather than an expected path.
 */
export function bucketEdges(edges: readonly DatedEdge[]): { buckets: EdgeBucket[]; unbucketed: number } {
  const byDay = new Map<string, { cutoff: string; edges: number }>();
  let unbucketed = 0;
  for (const edge of edges) {
    const iso = edge.ts_first;
    if (typeof iso !== 'string' || iso.length < 10 || Number.isNaN(Date.parse(iso))) {
      unbucketed += 1;
      continue;
    }
    const day = iso.slice(0, 10);
    const existing = byDay.get(day);
    if (existing === undefined) byDay.set(day, { cutoff: iso, edges: 1 });
    else {
      // Latest served instant wins, so the frame's cutoff includes every edge in the day.
      byDay.set(day, {
        cutoff: Date.parse(iso) >= Date.parse(existing.cutoff) ? iso : existing.cutoff,
        edges: existing.edges + 1,
      });
    }
  }
  const buckets = [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([utcDay, entry]) => ({ utcDay, cutoff: entry.cutoff, edges: entry.edges }));
  return { buckets, unbucketed };
}

/** Counts of the nodes carrying each overlay flag.
 *
 * Node-scoped on purpose: `_node_flags` (graph.py:336-352) writes a node's flags as the
 * union of the flags on the edges touching it plus `dense_community` and `flagged`, so an
 * edge that ever carried `cycle` surfaces it on both endpoints — and the chips highlight
 * nodes, so this counts what a chip would actually select. A flag nothing carries counts
 * zero, which the explorer renders as a disabled chip rather than a live toggle that
 * selects nothing. */
export function overlayCounts(nodes: readonly FlagCarrier[]): OverlayCounts {
  const carrying = (...flags: string[]): number =>
    nodes.filter((node) => flags.some((flag) => node.flags.includes(flag))).length;
  return {
    cycles: carrying(FLAG_CYCLE),
    high_velocity_hops: carrying(FLAG_HIGH_VELOCITY),
    fan_stars: carrying(FLAG_FAN_IN, FLAG_FAN_OUT),
    dense_communities: carrying(FLAG_DENSE_COMMUNITY),
    flagged: carrying(FLAG_FLAGGED),
  };
}
