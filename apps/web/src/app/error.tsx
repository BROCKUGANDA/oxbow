/* =============================================================================
   Tier 3 of the error ladder: the route-level boundary.

   Rendered by App Router when a segment throws and no closer boundary catches it. The
   stack trace goes to the console, not to the analyst: what they get is the HTTP
   status, the title the server sent, the run id with a copy button, and a reset that
   re-runs the segment rather than reloading the tab and losing everything else they
   had open.
   ============================================================================= */

'use client';

import type { ReactElement } from 'react';

import { RunIdChip } from '@/components/ui/provenance';
import { T_LABEL } from '@/components/ui/sx';
import { ErrorPane } from '@/design/primitives/ErrorPane';
import { failureDetail, failureRunId, failureTitle, toApiFailure } from '@/lib/api/problem';

export default function RouteError({
  error,
  reset,
}: { error: Error & { digest?: string }; reset: () => void }): ReactElement {
  const failure = toApiFailure(error);
  return (
    <div style={{ padding: 'var(--spacing-pane-gap)', display: 'flex', flexDirection: 'column', gap: 10 }}>
      <ErrorPane
        paneId="route"
        operation="This screen"
        error={{
          title: failure === null ? error.name || 'The route failed to render' : failureTitle(failure),
          detail: failure === null ? error.message : (failureDetail(failure) ?? undefined),
          run_id: failure === null ? (error.digest ?? undefined) : (failureRunId(failure) ?? undefined),
        }}
        onRetry={reset}
        siblingsIntact={false}
      >
        <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)', margin: 0 }}>
          The rest of the application is reachable from the header. Only this segment failed.
        </p>
      </ErrorPane>
      <RunIdChip runId={failure === null ? (error.digest ?? null) : failureRunId(failure)} />
    </div>
  );
}
