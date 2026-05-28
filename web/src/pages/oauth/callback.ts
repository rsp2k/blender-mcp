/**
 * GET /oauth/callback?code=...&state=...
 *
 * Receives the authorization code from mcp.blender.bet/authorize and
 * exchanges it for tokens. Steps:
 *   1. Read mcp_oauth_state cookie → {verifier, state, return_to, client_id}.
 *      Missing → user landed here without going through /oauth/start;
 *      treat as bad-request.
 *   2. Compare returned state to stored state (CSRF defense).
 *   3. POST /token with code + verifier + client_id.
 *   4. Write mcp_session cookie with the tokens; clear oauth_state cookie.
 *   5. 302 → return_to.
 *
 * Failures surface as a plain-text error page rather than a JSON 500 —
 * users land here in a browser, not via fetch.
 */
import type { APIRoute } from 'astro';

import { exchangeCodeForToken } from '../../lib/mcp-oauth';
import {
  clearOAuthStateCookie,
  readOAuthState,
  serializeSession,
} from '../../lib/session';

export const prerender = false;

function err(msg: string, status = 400): Response {
  return new Response(`OAuth callback error: ${msg}`, {
    status,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

export const GET: APIRoute = async ({ request, url }) => {
  const code = url.searchParams.get('code');
  const returnedState = url.searchParams.get('state');
  const oauthError = url.searchParams.get('error');

  if (oauthError) {
    const desc = url.searchParams.get('error_description') || '';
    return err(`upstream returned ${oauthError}: ${desc}`, 502);
  }
  if (!code || !returnedState) return err('missing code or state');

  const stateCookie = readOAuthState(request);
  if (!stateCookie) {
    return err('no in-flight oauth state cookie (did /oauth/start run?)');
  }
  if (stateCookie.state !== returnedState) {
    return err('state mismatch — possible CSRF, aborting');
  }

  let tok;
  try {
    tok = await exchangeCodeForToken({
      code,
      verifier: stateCookie.verifier,
      client_id: stateCookie.client_id,
    });
  } catch (e) {
    return err(`token exchange failed: ${(e as Error).message}`, 502);
  }

  const session = {
    access_token: tok.access_token,
    refresh_token: tok.refresh_token ?? '',
    expires_at: Math.floor(Date.now() / 1000) + tok.expires_in,
    client_id: stateCookie.client_id,
  };

  // Two Set-Cookie headers in one response: drop the in-flight state,
  // install the long-lived session. The Headers API supports `.append`
  // for repeated header names.
  const headers = new Headers({ Location: stateCookie.return_to });
  headers.append('Set-Cookie', serializeSession(session));
  headers.append('Set-Cookie', clearOAuthStateCookie());

  return new Response(null, { status: 302, headers });
};
