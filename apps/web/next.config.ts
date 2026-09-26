import type { NextConfig } from 'next';

/**
 * The web app talks to the API through one seam (`src/lib/api/transport.ts`), so the
 * only proxy rule here is the API prefix. Keeping it a proxy rather than a per-request
 * absolute URL means the browser never learns a second origin, which is what lets the
 * OIDC cookie session in plan §13 work without CORS exceptions.
 */
const API_BASE = process.env.OXBOW_API_ORIGIN ?? 'http://127.0.0.1:8000';

const config: NextConfig = {
  reactStrictMode: true,
  // Powers the "no route renders a value not in the API response" audit: sourcemaps
  // in CI only, never in a demo build shipped anywhere.
  productionBrowserSourceMaps: false,
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${API_BASE}/api/:path*` }];
  },
};

export default config;
