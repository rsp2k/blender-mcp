/**
 * POST /api/buses/[bus_id]/revoke (form-encoded: user_id=<target>)
 *
 * Owner-only — the MCP server's /api/buses/{bus_id}/revoke endpoint
 * checks ownership itself, so we just relay. On success, 302 back to
 * the detail page so the member list refreshes. On NeedsAuth, 302 to
 * /oauth/start with this URL as return_to.
 *
 * Form-encoded body (not JSON) because the trigger is a <form> submit
 * on the bus detail page — pulls in no client JS that way. action=POST,
 * one hidden input with the target user_id.
 */
import type { APIRoute } from 'astro';

import { mcpFetch, NeedsAuthError } from '../../../../lib/mcp-api';
import { serializeSession } from '../../../../lib/session';

export const prerender = false;

function html(msg: string, status: number): Response {
  return new Response(
    `<!doctype html><meta charset="utf-8"><title>${status}</title>` +
    `<style>body{font-family:system-ui;background:#0e1116;color:#d4d4d4;padding:2em;max-width:48em;margin:0 auto;}` +
    `pre{background:#161b22;padding:1em;border-radius:8px;overflow-x:auto;}</style>` +
    `<h1>${status}</h1><pre>${msg.replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]!))}</pre>` +
    `<p><a href="/buses/" style="color:#2bb3a4;">← Back to buses</a></p>`,
    { status, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
  );
}

export const POST: APIRoute = async ({ request, params }) => {
  const bus_id = params.bus_id ?? '';
  if (!/^[0-9a-f-]{36}$/i.test(bus_id)) return html('invalid bus_id', 400);

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return html('expected form-encoded body', 400);
  }
  const user_id = String(formData.get('user_id') ?? '').trim();
  if (!user_id) return html('user_id required', 400);

  let result;
  try {
    result = await mcpFetch(request, 'POST', `/api/buses/${bus_id}/revoke`, { user_id });
  } catch (e) {
    if (e instanceof NeedsAuthError) {
      const return_to = `/buses/${bus_id}/`;
      return new Response(null, {
        status: 302,
        headers: { Location: `/oauth/start?return_to=${encodeURIComponent(return_to)}` },
      });
    }
    return html(`mcpFetch failed: ${(e as Error).message}`, 500);
  }

  if (!result.response.ok) {
    const body = await result.response.text();
    return html(
      `Upstream returned ${result.response.status}: ${body.slice(0, 400)}`,
      result.response.status,
    );
  }

  // Success — 302 back to the bus detail page. Forward refreshed session
  // cookie if mcpFetch rotated tokens during the call.
  const headers = new Headers({ Location: `/buses/${bus_id}/` });
  if (result.refreshedSession) {
    headers.append('Set-Cookie', serializeSession(result.refreshedSession));
  }
  return new Response(null, { status: 303, headers });
};
