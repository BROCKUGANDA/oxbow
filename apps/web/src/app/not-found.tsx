/* A 404 that names what it could not find. The four empty states are for a region that
   is legitimately empty; this is a route that does not exist, and it says so plainly
   rather than borrowing an empty state's language. */

import Link from 'next/link';
import type { ReactElement } from 'react';

import { T_BODY, T_LABEL } from '@/components/ui/sx';

export default function NotFound(): ReactElement {
  return (
    <div style={{ padding: 'var(--spacing-pane-gap)', maxWidth: '64ch' }}>
      <h1 style={{ ...T_BODY, fontSize: 'var(--text-kpi)', fontWeight: 600 }}>No screen at this address</h1>
      <p style={{ ...T_LABEL, color: 'var(--color-ink-muted)' }}>
        The queue, the case workspace, the network explorer, the scorecard studio, the policy simulator and the
        validation page are reachable from the header.
      </p>
      <p style={{ marginTop: 12 }}>
        <Link href="/dashboard" style={{ ...T_LABEL, textDecoration: 'underline' }}>
          back to the command strip
        </Link>
      </p>
    </div>
  );
}
