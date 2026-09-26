#!/usr/bin/env python3
"""Admin CLI for personal access tokens (the bootstrap path for CI).

Users normally mint their own tokens with the ``blender_create_access_token``
MCP tool from an OAuth-signed-in client. This script is for the operator:
minting for a user who can't run that yet, never-expiring tokens, and
auditing or revoking. It writes the ``access_token`` table directly.

Tokens are tied to a user's OIDC ``sub`` (Authentik's hashed user id), the
same identity the bus uses. The database stores no usernames, so pick the
sub from ``users`` (bus owners) or from a token the user already has::

    docker compose exec -T blender-mcp uv run scripts/create_access_token.py users
    docker compose exec -T blender-mcp uv run scripts/create_access_token.py \\
        create --user-sub 2b09fa37... --name "github-actions" --expires-days 90
    docker compose exec -T blender-mcp uv run scripts/create_access_token.py list [--user-sub ...]
    docker compose exec -T blender-mcp uv run scripts/create_access_token.py revoke bmcp_Ab3xYz9Q

Reads DATABASE_URL from env (set by docker compose) and refuses to run
without it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import func, select

from blender_mcp import access_tokens as pat
from blender_mcp.storage import get_session
from blender_mcp.storage.models import Bus


async def _cmd_users() -> int:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(Bus.owner_user_id, func.count(), func.min(Bus.created_at))
                .group_by(Bus.owner_user_id)
                .order_by(func.min(Bus.created_at))
            )
        ).all()
    for sub, n, first in rows:
        print(f"{sub}  buses={n}  first_seen={first:%Y-%m-%d}")
    return 0


async def _cmd_create(user_sub: str, name: str, expires_days: int | None) -> int:
    async with get_session() as s:
        token, row = await pat.create_token(s, user_sub, name, expires_days)
    print(json.dumps({"token": token, **pat.to_dict(row)}, indent=2))
    print("\nShown once. Store it now (e.g. as the BLENDER_MCP_TOKEN secret).", file=sys.stderr)
    return 0


async def _cmd_list(user_sub: str | None) -> int:
    async with get_session() as s:
        rows = await pat.list_tokens(s, user_sub)
    for r in rows:
        d = pat.to_dict(r)
        print(f"{d['prefix']}  {d['status']:8}  {d['name']:30}  user={r.user_sub[:12]}  "
              f"expires={d['expires_at'] or 'never'}  last_used={d['last_used_at'] or '-'}  id={d['id']}")
    return 0


async def _cmd_revoke(key: str) -> int:
    async with get_session() as s:
        row = await pat.revoke_token(s, key)
    if row is None:
        print(f"No single token matches {key!r}", file=sys.stderr)
        return 1
    print(json.dumps(pat.to_dict(row), indent=2))
    print("Revoked; the server's verification cache drops it within 30 s.", file=sys.stderr)
    return 0


def main() -> int:
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL not set; run inside the server container.", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users", help="list known user subs (bus owners)")
    c = sub.add_parser("create", help="mint a token")
    c.add_argument("--user-sub", required=True)
    c.add_argument("--name", required=True)
    g = c.add_mutually_exclusive_group()
    g.add_argument("--expires-days", type=int, default=pat.DEFAULT_EXPIRES_DAYS)
    g.add_argument("--no-expiry", action="store_true")
    ls = sub.add_parser("list", help="list tokens")
    ls.add_argument("--user-sub")
    r = sub.add_parser("revoke", help="revoke by id or prefix")
    r.add_argument("key")
    a = p.parse_args()

    if a.cmd == "users":
        return asyncio.run(_cmd_users())
    if a.cmd == "create":
        return asyncio.run(_cmd_create(a.user_sub, a.name, None if a.no_expiry else a.expires_days))
    if a.cmd == "list":
        return asyncio.run(_cmd_list(a.user_sub))
    return asyncio.run(_cmd_revoke(a.key))


if __name__ == "__main__":
    sys.exit(main())
