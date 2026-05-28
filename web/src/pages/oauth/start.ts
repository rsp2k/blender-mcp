/**
 * GET /oauth/start?return_to=/buses/<id>
 *
 * Kicks off the PKCE flow with mcp.blender.bet. Steps:
 *   1. Ensure DCR has happened (cached after first call).
 *   2. Generate PKCE verifier + challenge + CSRF state.
 *   3. Stash {verifier, state, return_to, client_id} in the encrypted
 *      mcp_oauth_state cookie (10min expiry).
 *   4. 302 → mcp.blender.bet/authorize.
 *
 * The session cookie (the long-lived token blob) is untouched here —
 * /oauth/callback writes it after the token exchange succeeds.
 */
import { randomBytes } from 'node:crypto';

import type { APIRoute } from 'astro';

import {
  buildAuthorizeUrl,
  ensureClientRegistered,
  genPkce,
} from '../../lib/mcp-oauth';
import { serializeOAuthState } from '../../lib/session';

export const prerender = false;

export const GET: APIRoute = async ({ url }) => {
  // Sanity-check return_to: same-origin path only (no protocol-relative
  // or absolute URLs to avoid open-redirect). Default = /buses/.
  const rawReturnTo = url.searchParams.get('return_to') || '/buses/';
  const return_to =
    rawReturnTo.startsWith('/') && !rawReturnTo.startsWith('//')
      ? rawReturnTo
      : '/buses/';

  let client_id: string;
  try {
    client_id = await ensureClientRegistered();
  } catch (e) {
    return new Response(`DCR failed: ${(e as Error).message}`, { status: 502 });
  }

  const { verifier, challenge } = genPkce();
  const state = randomBytes(16).toString('base64url');

  const authorizeUrl = buildAuthorizeUrl({ client_id, challenge, state });

  return new Response(null, {
    status: 302,
    headers: {
      'Location': authorizeUrl,
      'Set-Cookie': serializeOAuthState({
        verifier,
        state,
        return_to,
        client_id,
      }),
    },
  });
};
