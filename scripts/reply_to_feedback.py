#!/usr/bin/env python3
"""Admin CLI for replying to LLM-submitted feedback.

v1 flow: no MCP tool to reply (would need role-gating we haven't built).
This script writes directly to the ``feedback`` table on the deploy host.

Typical use::

    # from the deploy host (dell01), inside the compose network
    docker compose exec -T blender-mcp uv run scripts/reply_to_feedback.py \\
        --id fr-Kf3nQp7Xy_A \\
        --author "Ryan" \\
        --text "Shipped in 1.5.22 as blender_set_active_camera." \\
        --status resolved

or interactively (prompts for text on stdin)::

    docker compose exec -it blender-mcp uv run scripts/reply_to_feedback.py \\
        --id fr-Kf3nQp7Xy_A --author "Ryan" --status in-progress

Reads DATABASE_URL from env — set by docker compose. Refuses to run
without it rather than fall back to a local sqlite so an operator can't
accidentally reply to a wrong DB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from blender_mcp.storage import get_session
from blender_mcp.storage.feedback_repo import (
    append_reply,
    get_feedback,
    list_feedback,
    to_dict,
    update_status,
)
from blender_mcp.storage.models import FeedbackStatus


async def _cmd_show(feedback_id: str) -> int:
    async with get_session() as session:
        row = await get_feedback(session, feedback_id)
    if row is None:
        print(f"NOT FOUND: {feedback_id}", file=sys.stderr)
        return 1
    print(json.dumps(to_dict(row), indent=2))
    return 0


async def _cmd_list(status: str | None, category: str | None, limit: int) -> int:
    async with get_session() as session:
        rows = await list_feedback(
            session,
            status=FeedbackStatus(status) if status else None,
            category=None if category is None else _cat(category),
            limit=limit,
        )
    for r in rows:
        # Compact one-line-per-row summary; use --show for full detail.
        stat = r.status.value.ljust(11)
        cat = r.category.value.ljust(15)
        title = r.title[:60]
        print(f"{r.id:20}  {stat}  {cat}  {title}")
    print(f"({len(rows)} row(s))")
    return 0


async def _cmd_reply(
    feedback_id: str,
    author: str,
    text: str | None,
    status: str | None,
    resolution: str | None,
) -> int:
    # Interactive text-read when --text omitted; keeps multiline replies
    # legible without shell-escaping every backtick and quote.
    if text is None:
        print(f"Enter reply text for {feedback_id} (finish with Ctrl-D):", file=sys.stderr)
        text = sys.stdin.read().strip()
        if not text:
            print("Empty reply, aborting.", file=sys.stderr)
            return 1

    async with get_session() as session:
        row = await append_reply(
            session, feedback_id, author=author, text=text
        )
        if row is None:
            print(f"NOT FOUND: {feedback_id}", file=sys.stderr)
            return 1
        if status is not None:
            row = await update_status(
                session, feedback_id,
                status=FeedbackStatus(status),
                resolution=resolution,
            )

    print(json.dumps(to_dict(row), indent=2))
    return 0


def _cat(raw: str):
    from blender_mcp.storage.models import FeedbackCategory
    return FeedbackCategory(raw)


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print(
            "ERROR: DATABASE_URL not set. Run this script inside the "
            "blender-mcp container (where compose injects it) or export "
            "it explicitly.",
            file=sys.stderr,
        )
        return 2

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="Print one feedback row as JSON.")
    p_show.add_argument("id")

    p_list = sub.add_parser("list", help="Newest-first summary of feedback rows.")
    p_list.add_argument("--status", choices=[s.value for s in FeedbackStatus])
    p_list.add_argument(
        "--category",
        choices=["feature-request", "bug", "friction", "other"],
    )
    p_list.add_argument("--limit", type=int, default=50)

    p_reply = sub.add_parser("reply", help="Append a reply, optionally change status.")
    p_reply.add_argument("--id", required=True)
    p_reply.add_argument("--author", required=True)
    p_reply.add_argument("--text", help="Reply body (omit to read from stdin).")
    p_reply.add_argument(
        "--status",
        choices=[s.value for s in FeedbackStatus],
        help="Optionally update the status in the same call.",
    )
    p_reply.add_argument("--resolution", help="Optional resolution note.")

    args = p.parse_args()

    if args.cmd == "show":
        return asyncio.run(_cmd_show(args.id))
    if args.cmd == "list":
        return asyncio.run(_cmd_list(args.status, args.category, args.limit))
    if args.cmd == "reply":
        return asyncio.run(
            _cmd_reply(args.id, args.author, args.text, args.status, args.resolution)
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
