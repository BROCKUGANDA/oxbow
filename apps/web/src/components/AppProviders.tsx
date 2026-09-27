/* =============================================================================
   Client providers, in the order their guarantees matter.

   * QueryClient is created once per browser tab, not per render, and it is the only
     place the retry policy is installed. A per-render client would refetch on every
     keystroke, which is how a queue becomes a load test.
   * MotionConfig with reducedMotion="user" is the global guard DESIGN.md §4 names.
     The `?motion=reduced` override is applied by `useMotionOverride` in the shell,
     because a query parameter has to be readable without changing the operating
     system — that is what turns the reduced-motion guarantee into a tested path.
   * The theme is an attribute on <html>. No component ever branches on it.
   ============================================================================= */

'use client';

import { QueryClientProvider } from '@tanstack/react-query';
import { MotionConfig } from 'motion/react';
import { type ReactElement, type ReactNode, createContext, useContext, useMemo, useState } from 'react';

import { createQueryClient } from '../lib/api/client';

type ThemeName = 'canvas' | 'paper';

type ShellState = {
  theme: ThemeName;
  setTheme: (next: ThemeName) => void;
  reducedMotion: boolean;
  setReducedMotion: (next: boolean) => void;
};

const ShellContext = createContext<ShellState | null>(null);

export function useShell(): ShellState {
  const value = useContext(ShellContext);
  if (value === null) throw new Error('useShell must be called inside AppProviders');
  return value;
}

export function AppProviders({ children }: { children: ReactNode }): ReactElement {
  const queryClient = useMemo(() => createQueryClient(), []);
  const [theme, setTheme] = useState<ThemeName>('canvas');
  const [reducedMotion, setReducedMotion] = useState(false);

  return (
    <QueryClientProvider client={queryClient}>
      <ShellContext.Provider value={{ theme, setTheme, reducedMotion, setReducedMotion }}>
        {/* reducedMotion="user" honours the OS setting; the state below is the
            gallery's explicit override, and either one being true is enough. */}
        <MotionConfig reducedMotion={reducedMotion ? 'always' : 'user'}>
          <ThemeEffect theme={theme}>{children}</ThemeEffect>
        </MotionConfig>
      </ShellContext.Provider>
    </QueryClientProvider>
  );
}

/** Reflects the theme onto <html data-theme>, which is where the token file looks. */
function ThemeEffect({ theme, children }: { theme: ThemeName; children: ReactNode }): ReactElement {
  if (typeof document !== 'undefined' && document.documentElement.dataset.theme !== theme) {
    document.documentElement.dataset.theme = theme;
  }
  return <>{children}</>;
}
