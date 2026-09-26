/* =============================================================================
   /dev/states — the page entry. It is a server component for exactly one reason:
   reading `?motion=reduced` from `searchParams` here means the client gallery
   never calls `useSearchParams`, the route never needs a Suspense boundary for
   it, and the gallery is in the first server-painted frame at its final height.
   The earlier shape — a client page suspending on the query string, with a
   ten-row skeleton as the fallback — painted the fallback and then swapped in
   the full gallery: a measured 0.138 CLS on the very page that documents the
   zero-CLS claim. The split is the fix; the before/after measurement is the
   evidence.
   ============================================================================= */

import type { ReactElement } from 'react';

import { StatesGallery } from './gallery';

type Props = {
  searchParams: Promise<{ motion?: string }>;
};

export default async function StatesPage({ searchParams }: Props): Promise<ReactElement> {
  const params = await searchParams;
  return <StatesGallery reduced={params.motion === 'reduced'} />;
}
