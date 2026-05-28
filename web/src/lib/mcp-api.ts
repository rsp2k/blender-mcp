/**
 * Authenticated REST wrapper around mcp.blender.bet/api/*.
 *
 * Lifecycle:
 *   1. Caller invokes mcpFetch(request, 'POST', '/api/buses/.../revoke', body).
 *   2. We read the session cookie. Missing → throw NeedsAuth — caller
 *      should 302 to /oauth/start?return_to=<original-url>.
 *   3. Token within 60s of expiry → server-side refresh BEFORE the call;
 *      updated session is returned alongside the response so the route
 *      handler can write a refreshed Set-Cookie header.
 *   4. Call /api/* with Authorization: Bearer; on 401 we retry once
 *      after a forced refresh, which covers the "token revoked
 *      out-of-band" case.
 *
 * Returns the raw Response object — callers handle response.ok and JSON
 * parsing themselves. Returns also the (possibly-refreshed) session so
 * route handlers can decide whether to write a Set-Cookie header.
 */
import {
  MCP_SERVER_URL,
  refreshAccessToken,
} from './mcp-oauth';
import {
  type McpSession,
  readSession,
} from './session';

export class NeedsAuthError extends Error {
  constructor(public readonly reason: string) {
    super(`OAuth required: ${reason}`);
    this.name = 'NeedsAuthError';
  }
}

export interface McpFetchResult {
  response: Response;
  /**
   * Set when the session was refreshed during this call. Route handlers
   * should serialize this back into a Set-Cookie header on their response
   * so the rotated tokens are persisted to the browser.
   */
  refreshedSession: McpSession | null;
}

async function refreshSession(session: McpSession): Promise<McpSession> {
  const tok = await refreshAccessToken({
    refresh_token: session.refresh_token,
    client_id: session.client_id,
  });
  return {
    access_token: tok.access_token,
    refresh_token: tok.refresh_token ?? session.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + tok.expires_in,
    client_id: session.client_id,
  };
}

export async function mcpFetch(
  request: Request,
  method: string,
  path: string,
  body?: unknown,
): Promise<McpFetchResult> {
  let session = readSession(request);
  if (!session) throw new NeedsAuthError('no session cookie');

  let refreshed: McpSession | null = null;

  // Proactive refresh — within 60s of expiry, swap before calling.
  const now = Math.floor(Date.now() / 1000);
  if (session.expires_at - now < 60) {
    try {
      session = await refreshSession(session);
      refreshed = session;
    } catch (e) {
      // Refresh token bad / revoked — force re-auth.
      throw new NeedsAuthError(`refresh failed: ${(e as Error).message}`);
    }
  }

  const url = `${MCP_SERVER_URL}${path}`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${session.access_token}`,
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // 401 → token revoked out-of-band. Retry once after refresh.
  if (response.status === 401 && refreshed === null) {
    try {
      session = await refreshSession(session);
      refreshed = session;
    } catch (e) {
      throw new NeedsAuthError(`401 + refresh failed: ${(e as Error).message}`);
    }
    response = await fetch(url, {
      method,
      headers: { ...headers, Authorization: `Bearer ${session.access_token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  return { response, refreshedSession: refreshed };
}
