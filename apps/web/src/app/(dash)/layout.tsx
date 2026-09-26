/* =============================================================================
   The dashboard route group's layout.

   It exists so the seven product screens share one scroll container and one error
   segment, and so `error.tsx` beside it catches a failure in any of them without
   catching a failure in `/dev/states` — plan §14's tier-3 requirement is a route-level
   boundary *per segment*, which in App Router means one `error.tsx` per group.
   ============================================================================= */

import type { ReactElement, ReactNode } from 'react';

export default function DashLayout({ children }: { children: ReactNode }): ReactElement {
  return <div data-route-group="dash">{children}</div>;
}
