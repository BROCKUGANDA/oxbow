/* =============================================================================
   Standing copy. Prose the plan fixes verbatim, so it is data-adjacent rather than
   decorative: the disclaimer is the same text the README carries, the packet's page
   one, and `test_disclaimer_present_everywhere` asserts all three.

   A screen renders `meta.disclaimer` when a payload carries it — the disclaimer
   travels with the payload, which is the point of the field — and falls back to this
   constant so the footer is never blank while the API is down. An empty footer is
   the one way this product could fail to say what it is.
   ============================================================================= */

export const DISCLAIMER =
  'OXBOW is a research prototype that analyzes historical, de-identified data only. It does not process live ' +
  'financial transactions, does not trade or advise on any financial instrument, does not make real financial ' +
  'decisions, and is not financial advice. Monetary figures are model estimates derived from stated assumptions, ' +
  'not measured outcomes. Results are not validated for operational use by any financial institution.';

/** Required by plan §15 alongside the disclaimer: the scenario-dressing note. */
export const SCENARIO_NOTE =
  'Any East-African framing in this interface is illustrative scenario dressing over permitted public data, not a ' +
  'claim about any real institution, market or regulator.';

export const PIPELINE_COMMAND = 'make pipeline';

/** Named so a reviewer can see it is copy, not a measured duration: the estimate the
 *  fresh-install empty state prints, which the API may override per deployment. */
export const RUNTIME_ESTIMATE_FALLBACK = '6–9 minutes on the dev slice';
