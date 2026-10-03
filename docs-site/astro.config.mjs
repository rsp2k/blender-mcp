// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';
import icon from 'astro-icon';

// HMR-behind-Caddy configuration (see ~/.claude/CLAUDE.md HMR section).
// In dev-behind-proxy mode docker-compose sets these to the public hostname,
// `wss`, and 443 so the browser can reach the dev WebSocket via Caddy.
const HMR_HOST = process.env.HMR_HOST || 'localhost';
const HMR_PROTOCOL = process.env.HMR_PROTOCOL || 'ws';
const HMR_CLIENT_PORT = Number(process.env.HMR_CLIENT_PORT || 4321);

const SITE = process.env.SITE_URL || 'https://docs.blender.bet';

// https://astro.build/config
export default defineConfig({
  site: SITE,
  devToolbar: { enabled: false },
  integrations: [
    // Must precede starlight() so Starlight uses our customized instance
    // instead of auto-registering its own. Excludes /login-complete from
    // the sitemap — auth-gated, would 401 for crawlers.
    sitemap({
      filter: (page) => !page.includes('/login-complete'),
    }),
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
        src: './src/assets/clip-logo.png',
        alt: 'B. Clip, the BlenderMCP mascot',
        replacesTitle: false,
      },
      favicon: '/favicon-32.png',
      // B. Clip's pupils follow the pointer in the header logo.
      components: {
        SiteTitle: './src/components/SiteTitle.astro',
        Footer: './src/components/Footer.astro',
      },
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
      // A user's guide first; material for people building their own MCP
      // clients sits in one collapsed group at the end. Internal and admin
      // notes live in the repository's docs/ folder, not on this site.
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'Overview', slug: 'index' },
            { slug: 'tutorials/get-blender' },
            { slug: 'tutorials/quickstart' },
          ],
        },
        {
          label: 'Using BlenderMCP',
          items: [
            { slug: 'how-to/chat-in-blender' },
            { slug: 'how-to/claude-api-key' },
            { slug: 'how-to/tool-servers' },
            { slug: 'how-to/install-addon' },
            { slug: 'how-to/ci-and-scripts' },
          ],
        },
        {
          label: 'For developers',
          collapsed: true,
          items: [
            {
              label: 'Guides',
              items: [
                { slug: 'how-to/write-llm-client' },
                { slug: 'how-to/long-running-jobs' },
                { slug: 'how-to/background-workers' },
                { slug: 'how-to/use-capabilities' },
              ],
            },
            {
              label: 'Tool reference',
              items: [
                { slug: 'reference/dispatch-tools' },
                { slug: 'reference/data-tools' },
                { slug: 'reference/file-scene-tools' },
                { slug: 'reference/view-render-tools' },
                { slug: 'reference/polyhaven-texture-tools' },
                { slug: 'reference/bus-tools' },
                { slug: 'reference/resources' },
                { slug: 'reference/prompts' },
                { slug: 'reference/auth' },
                { slug: 'reference/chat-protocol' },
                { slug: 'reference/stage-snapshot' },
              ],
            },
            {
              label: 'How it works',
              items: [
                { slug: 'explanation/architecture' },
                { slug: 'explanation/dispatch-vs-script' },
                { slug: 'explanation/oauth-buses' },
                { slug: 'explanation/use-cases' },
              ],
            },
          ],
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
