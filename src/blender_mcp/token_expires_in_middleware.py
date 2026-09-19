"""Ensure /token responses always include the ``expires_in`` field.

OAuth 2.0 §4.2.2 SHOULD (not MUST) return ``expires_in`` in successful
token responses. FastMCP's OIDCProxy sometimes omits it when the
upstream Authentik reply lacks it, which breaks strict OAuth clients
that use ``expires_in`` to schedule their refresh. The BlenderMCP
addon hit this: its ``_refresh_watcher`` read 0, fell into the
"unknown expiry" branch, slept 3600s, and the session died at the
real token TTL (verified against feedback bug-4Cg1LPmzULs's sibling
symptom "auth expires every hour"). The addon now has a JWT-exp
fallback of its own, but any other MCP client (Claude Desktop, a
generic OAuth 2.0 library) would still misbehave.

This Starlette HTTP middleware fixes it centrally on the server so
every client benefits. On any ``POST /token`` response whose body is
JSON with ``access_token`` but no ``expires_in``, decode the access
token's ``exp`` claim (every JWT carries one) and inject
``expires_in = exp - now``. Non-token responses and non-JSON bodies
pass through untouched.

Design notes:

- Registered as ``@app.middleware("http")`` in oauth_server.py,
  same pattern as jwt_middleware. No FastMCP private-API contact.
- Reads/rewrites the entire response body in memory. Token responses
  are always small (< 4 KB), so buffering is cheap.
- Signature validation is intentionally skipped when decoding the
  access token's ``exp`` claim: we're not making an auth decision,
  we're just reading a self-declared expiry to populate a spec-hint
  field. If the token is malformed we pass the response through
  unchanged rather than crash.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


def _decode_jwt_exp(access_token: str) -> int | None:
    """Read the ``exp`` claim from an access token. Returns None on any
    parse failure so the middleware falls back to the pass-through path.

    No signature check: we're only reading the expiry for informational
    field population, not granting privilege.
    """
    if not access_token or not isinstance(access_token, str):
        return None
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp)
    except Exception:
        return None
    return None


async def ensure_expires_in(request: Request, call_next):
    """Wrap the response to /token; inject expires_in when missing.

    Meant to be registered via ``@app.middleware("http")``. The wrapping
    only fires for POST /token; every other request path is a
    single-line pass-through.
    """
    response = await call_next(request)

    # Fast path: only care about POST /token JSON responses. Anything
    # else, hand back the response object unchanged.
    if request.url.path != "/token" or request.method != "POST":
        return response
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        return response
    if response.status_code >= 400:
        # Error responses don't carry access_token; nothing to fix.
        return response

    # Starlette streams responses via body_iterator; we need to buffer
    # the whole body to parse + rewrite. Token responses are always
    # small (a few KB), so this is safe.
    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not JSON despite the content-type — pass through as-is so we
        # don't corrupt something weird upstream is doing.
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=content_type,
        )

    if isinstance(payload, dict) and payload.get("access_token") and not payload.get("expires_in"):
        exp = _decode_jwt_exp(payload["access_token"])
        if exp is not None:
            remaining = exp - int(time.time())
            if remaining > 0:
                payload["expires_in"] = remaining
                logger.info(
                    "token_middleware: injected expires_in=%d from JWT exp claim",
                    remaining,
                )
                body = json.dumps(payload).encode("utf-8")
                # Content-length must match the new body length or Starlette
                # / httpx clients complain about truncated reads.
                new_headers = dict(response.headers)
                new_headers["content-length"] = str(len(body))
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=new_headers,
                    media_type=content_type,
                )

    # No mutation needed (or fallback failed) — reconstruct the response
    # from the buffered body since we already consumed body_iterator.
    return Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=content_type,
    )
