/**
 * MCP server OAuth client — DCR, PKCE, token exchange, refresh.
 *
 * Mirrors addon/auth/oauth_pkce.py in TypeScript so the web/ BFF can
 * become an OAuth client of mcp.blender.bet on equal footing with the
 * Blender addon. RFC 7591 DCR (no static client_secret), RFC 8252 PKCE
 * (no public-client secrets in flight), token_endpoint_auth_method=none.
 *
 * Cold-start cost: one DCR call per server boot. The result is cached
 * in module scope so subsequent /oauth/start hits skip the round trip.
 * On server restart we re-DCR — Authentik issues a fresh client_id but
 * accepts the same redirect_uri, so users in mid-flow are not affected
 * (their pending state cookie carries the OLD client_id, which still
 * works for the in-flight token exchange).
 */
import { createHash, randomBytes } from 'node:crypto';

const MCP_SERVER_URL = (process.env.MCP_SERVER_URL || 'https://mcp.blender.bet').replace(/\/+$/, '');
const SITE_URL = (process.env.SITE_URL || 'https://blender.bet').replace(/\/+$/, '');
const REDIRECT_URI = `${SITE_URL}/oauth/callback`;

// Static identifier so the MCP server's role-attribution middleware can
// tag this client. software_id "blender-mcp-web" is not in the known map,
// so falls through to llm-client — which is exactly right for a web BFF
// that initiates dispatch.
const SOFTWARE_ID = 'blender-mcp-web';
const SOFTWARE_VERSION = '2026.05.27';

let _cachedClientId: string | null = null;
let _registrationPromise: Promise<string> | null = null;

/* ---- DCR ------------------------------------------------------------ */

async function _doRegister(): Promise<string> {
  const resp = await fetch(`${MCP_SERVER_URL}/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_name: 'BlenderMCP Web',
      redirect_uris: [REDIRECT_URI],
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
      software_id: SOFTWARE_ID,
      software_version: SOFTWARE_VERSION,
    }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`DCR failed: HTTP ${resp.status} — ${body.slice(0, 200)}`);
  }
  const data = await resp.json();
  if (!data.client_id) throw new Error(`DCR response missing client_id`);
  return data.client_id as string;
}

/** Return the cached client_id, performing DCR on first call. */
export async function ensureClientRegistered(): Promise<string> {
  if (_cachedClientId) return _cachedClientId;
  // Coalesce concurrent first-call races into one DCR request.
  if (_registrationPromise) return _registrationPromise;
  _registrationPromise = (async () => {
    const cid = await _doRegister();
    _cachedClientId = cid;
    _registrationPromise = null;
    return cid;
  })();
  return _registrationPromise;
}

/* ---- PKCE ----------------------------------------------------------- */

function base64url(buf: Buffer): string {
  return buf.toString('base64url');
}

/** Generate (verifier, challenge) per RFC 7636 §4.1/§4.2 — S256. */
export function genPkce(): { verifier: string; challenge: string } {
  const verifier = base64url(randomBytes(32));
  const challenge = base64url(createHash('sha256').update(verifier).digest());
  return { verifier, challenge };
}

/* ---- authorize URL -------------------------------------------------- */

export function buildAuthorizeUrl(opts: {
  client_id: string;
  challenge: string;
  state: string;
}): string {
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: opts.client_id,
    redirect_uri: REDIRECT_URI,
    code_challenge: opts.challenge,
    code_challenge_method: 'S256',
    state: opts.state,
  });
  return `${MCP_SERVER_URL}/authorize?${params}`;
}

/* ---- token exchange + refresh -------------------------------------- */

interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  expires_in: number;
  token_type: string;
}

export async function exchangeCodeForToken(opts: {
  code: string;
  verifier: string;
  client_id: string;
}): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code: opts.code,
    redirect_uri: REDIRECT_URI,
    client_id: opts.client_id,
    code_verifier: opts.verifier,
  });
  const resp = await fetch(`${MCP_SERVER_URL}/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!resp.ok) {
    const errBody = await resp.text();
    throw new Error(`Token exchange failed: HTTP ${resp.status} — ${errBody.slice(0, 200)}`);
  }
  const data = await resp.json();
  if (!data.access_token) throw new Error(`Token response missing access_token`);
  return data as TokenResponse;
}

export async function refreshAccessToken(opts: {
  refresh_token: string;
  client_id: string;
}): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: opts.refresh_token,
    client_id: opts.client_id,
  });
  const resp = await fetch(`${MCP_SERVER_URL}/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!resp.ok) {
    const errBody = await resp.text();
    throw new Error(`Refresh failed: HTTP ${resp.status} — ${errBody.slice(0, 200)}`);
  }
  const data = await resp.json();
  if (!data.access_token) throw new Error(`Refresh response missing access_token`);
  return data as TokenResponse;
}

export { MCP_SERVER_URL };
