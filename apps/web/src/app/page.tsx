/* The root is a redirect, not a landing page. This is an investigation tool with one
   opening screen — the command strip — and a marketing page in front of it would cost
   the demo its first twenty seconds. */

import { redirect } from 'next/navigation';
import type { ReactElement } from 'react';

export default function RootPage(): ReactElement {
  redirect('/dashboard');
}
