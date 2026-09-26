/* The gallery needs a `meta` block for the states that a live API has not produced —
   the degraded banner is one of them. These are the strings the transport itself stamps,
   so the gallery demonstrates the component against the same shape the wire sends. */

import type { Meta } from '@/lib/api/contract';
import { DISCLAIMER } from '@/lib/copy';

export const GALLERY_META: Meta = {
  run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
  trace_id: '01J4Z7TRACE00000000000000',
  model_version: 'lgbm-fusion-4.5.3+isotonic',
  provenance: 'fixture:developer-contract',
  generated_at: '2026-09-25T06:12:00Z',
  assumptions: [
    { key: 'recovery.rate', value: 0.35, source: 'config/economics.yaml', note: 'illustrative' },
    { key: 'analyst.cost_per_minute_minor', value: 15000, source: 'config/economics.yaml', note: null },
  ],
  degraded: true,
  degraded_reason: 'The solver port did not answer inside its deadline, so the greedy allocation is shown.',
  disclaimer: DISCLAIMER,
};
