// @ts-check
import { defineConfig } from 'astro/config';
import node from '@astrojs/node';

// Public site URL — used for canonical links + sitemap. Override per-env
// via SITE_URL.
const SITE = process.env.SITE_URL || 'https://blender.bet';

// HMR-behind-Caddy plumbing (mirrors docs-site config). When the dev
// container runs behind the host Caddy on a public hostname, Vite has to
// advertise the WebSocket via wss:// + the public port (443) or the
// browser can't reach the HMR socket.
const HMR_HOST = process.env.HMR_HOST || 'localhost';
const HMR_PROTOCOL = process.env.HMR_PROTOCOL || 'ws';
const HMR_CLIENT_PORT = Number(process.env.HMR_CLIENT_PORT || 4321);

// https://astro.build/config
export default defineConfig({
  site: SITE,
  devToolbar: { enabled: false },

  // SSR-first ('server') because the primary value of this site is the
  // /login-complete page reading Authentik forward_auth headers. A few
  // routes (homepage) opt back into static via `export const prerender =
  // true` for caching.
  output: 'server',
  adapter: node({ mode: 'standalone' }),

  // /login-complete reads Authentik headers and renders — it never
  // mutates state. CSRF protection here rejects local-test 127.0.0.1
  // probes with 403 with no benefit. Disable globally; if we ever add a
  // POST endpoint, add explicit CSRF token check there.
  security: { checkOrigin: false },

  vite: {
    server: {
      host: '0.0.0.0',
      hmr: {
        host: HMR_HOST,
        protocol: HMR_PROTOCOL,
        clientPort: HMR_CLIENT_PORT,
      },
      allowedHosts: true,
    },
  },
});
