// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import icon from 'astro-icon';

// HMR-behind-Caddy configuration (see ~/.claude/CLAUDE.md HMR section).
// In dev-behind-proxy mode docker-compose sets these to the public hostname,
// `wss`, and 443 so the browser can reach the dev WebSocket via Caddy.
const HMR_HOST = process.env.HMR_HOST || 'localhost';
const HMR_PROTOCOL = process.env.HMR_PROTOCOL || 'ws';
const HMR_CLIENT_PORT = Number(process.env.HMR_CLIENT_PORT || 4321);

const SITE = process.env.SITE_URL || 'https://blender.bet';

// https://astro.build/config
export default defineConfig({
  site: SITE,
  devToolbar: { enabled: false },
  integrations: [
    icon({
      include: {
        // Pull only the lucide icons we actually use to keep the bundle lean.
        // Add to this list rather than wildcarding — the build complains otherwise.
        lucide: [
          'share-2',
          'workflow',
          'compass',
          'book-open',
          'wrench',
          'lightbulb',
          'package',
          'shield-check',
          'message-square',
          'users',
          'rocket',
          'github',
        ],
      },
    }),
    starlight({
      title: 'BlenderMCP',
      description:
        'Multiple LLM clients collaborating on shared Blender 3D instances over the Model Context Protocol.',
      logo: {
        src: './src/assets/logo.svg',
        alt: 'BlenderMCP — connected nodes',
        replacesTitle: false,
      },
      favicon: '/favicon.svg',
      customCss: ['./src/styles/custom.css'],
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/rsp2k/blender-mcp',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/rsp2k/blender-mcp/edit/main/docs-site/',
      },
      lastUpdated: true,
      pagination: true,
      sidebar: [
        {
          label: 'Start here',
          items: [{ label: 'Overview', slug: 'index' }],
        },
        {
          label: 'Tutorials',
          items: [{ autogenerate: { directory: 'tutorials' } }],
        },
        {
          label: 'How-to guides',
          items: [{ autogenerate: { directory: 'how-to' } }],
        },
        {
          label: 'Reference',
          items: [{ autogenerate: { directory: 'reference' } }],
        },
        {
          label: 'Explanation',
          items: [{ autogenerate: { directory: 'explanation' } }],
        },
      ],
    }),
  ],
  vite: {
    server: {
      host: '0.0.0.0',
      hmr: {
        host: HMR_HOST,
        protocol: HMR_PROTOCOL,
        clientPort: HMR_CLIENT_PORT,
      },
      // Required so Vite trusts the public hostname proxied by Caddy
      allowedHosts: true,
    },
  },
});
