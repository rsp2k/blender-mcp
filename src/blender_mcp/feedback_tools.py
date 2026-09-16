"""MCP tools for structured feedback capture.

Three tools:

- ``blender_submit_feedback`` — record a feature request / bug / friction
  point. Returns the shortcode the submitter can share and re-query.
- ``blender_get_feedback`` — fetch one submission by shortcode, including
  any replies from the admin.
- ``blender_list_feedback`` — enumerate submissions. Transparent v1:
  every authenticated user sees every submission (no user_id filter).
  Filters on category, status, and optional submitter_user_id are
  available for opt-in narrowing ("show me only my open items").

Reply-side workflow is admin-only (no MCP tool to append replies in v1).
See scripts/reply_to_feedback.py — small CLI that writes directly to
the DB and updates status. If reply volume ever justifies a web UI, the
schema is already shaped for it (JSONB replies array, indexed status).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _resolve_user_id
from .storage import get_session
from .storage.feedback_repo import (
    create_feedback,
    get_feedback,
    list_feedback,
    to_dict,
)
from .storage.models import FeedbackCategory, FeedbackStatus

logger = logging.getLogger(__name__)


# Bound the response payload for list_feedback so a large table doesn't
# blow through MCP tool-result budgets. 50 rows @ few-KB each is safe.
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 200


# Sentinel for "caller passed an unknown value" — distinct from "caller
# passed None to mean no filter". Callers convert this into a structured
# error rather than silently coercing to a default (which was the pre-fix
# behavior: unknown category → silent fallback to friction).
class _Unknown:
    pass
UNKNOWN = _Unknown()


def _parse_category(raw: Optional[str]):
    """Returns None (no value given), UNKNOWN (bad value), or the enum."""
    if raw is None:
        return None
    try:
        return FeedbackCategory(raw)
    except ValueError:
        return UNKNOWN


def _parse_status(raw: Optional[str]):
    """Returns None (no value given), UNKNOWN (bad value), or the enum."""
    if raw is None:
        return None
    try:
        return FeedbackStatus(raw)
    except ValueError:
        return UNKNOWN


_VALID_CATEGORIES = [c.value for c in FeedbackCategory]
_VALID_STATUSES = [s.value for s in FeedbackStatus]


class BlenderFeedbackComponent(MCPMixin):
    """Feedback / feature-request tools, not role-gated in v1.

    Any authenticated caller can submit; any authenticated caller can
    read (transparent). If ever we want to gate replies to admins, that
    happens in feedback_repo, not here.
    """

    @mcp_tool()
    async def submit_feedback(
        self,
        title: str,
        body: str,
        category: str = "friction",
        context: Optional[dict] = None,
        submitter_client_uuid: Optional[str] = None,
        submitter_client_label: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Record structured feedback about the tool surface.

        When to reach for this: whenever you find yourself writing
        ``blender_execute_code`` for something that FEELS like it
        should be a first-class tool, submit a feature-request first
        so we capture the gap. Also for bugs, workflow friction, and
        anything else you'd normally tell the maintainer in chat.

        Args:
            title: One-sentence summary shown in listings.
            body: Full description. Include what you tried, what
                happened, what you expected. No length limit — err
                on the side of detail.
            category: "feature-request" (default expectation for
                "this tool is missing"), "bug", "friction", or "other".
                Defaults to "friction" — the safest bucket if you're
                unsure.
            context: Free-form JSON with anything else useful:
                {"attempted_tool": "blender_execute_code",
                 "attempted_code": "...",
                 "error": "...",
                 "addon_version": "1.5.20",
                 "blender_version": "5.2"}.
            submitter_client_uuid: Your stable LLM UUID (optional).
                Useful if you'd like your own submissions grouped when
                someone reads the list; reuse across sessions.
            submitter_client_label: Human-readable name (e.g.,
                "Claude Desktop", "Ryan's terminal"). Shown in listings.

        Returns JSON string with the shortcode (``id``) you should
        share when referencing this submission later, plus the browser
        URL for the human's convenience.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "reason": "unauthenticated"})

        cat = _parse_category(category)
        if isinstance(cat, _Unknown):
            return json.dumps({
                "status": "error",
                "reason": "unknown_category",
                "given": category,
                "accepted": _VALID_CATEGORIES,
            })
        if cat is None:
            cat = FeedbackCategory.friction

        # Both title and body are required non-empty; a truly empty
        # submission is almost always a client-side bug rather than a
        # genuine "no words to add" case.
        if not (title or "").strip() or not (body or "").strip():
            return json.dumps({
                "status": "error",
                "reason": "title_and_body_required",
            })

        try:
            async with get_session() as session:
                row = await create_feedback(
                    session,
                    submitter_user_id=user_id,
                    submitter_client_uuid=submitter_client_uuid,
                    submitter_client_label=submitter_client_label,
                    category=cat,
                    title=title.strip(),
                    body=body,
                    context=context or {},
                )
        except Exception as exc:
            # Defense-in-depth: even with values_callable on the enum
            # column, any future schema mismatch or DB-level failure
            # would previously dump SQLAlchemy's full error including
            # the INSERT statement, bound params, and submitter user_id
            # hash to the MCP caller. Log the detail server-side, return
            # a scrubbed structured error to the caller. Verified in
            # bug-pYH00BElpAs. See also FeedbackCategory.feature_request
            # → previously "feature_request" reached Postgres by mistake.
            logger.exception("submit_feedback failed for user_id=%s category=%s", user_id, cat.value)
            return json.dumps({
                "status": "error",
                "reason": "storage_failure",
                "hint": "The submission couldn't be recorded. Server-side logs have the detail. Try again; if it persists, report to the maintainer out-of-band.",
            })

        return json.dumps({
            "status": "ok",
            "id": row.id,
            "category": row.category.value,
            "url": f"https://mcp.blender.bet/feedback/{row.id}",
        })

    @mcp_tool()
    async def get_feedback(
        self,
        feedback_id: str,
        ctx: Context = None,
    ) -> str:
        """Fetch one feedback submission by shortcode, including replies.

        Use this to check whether a prior submission of yours has
        gotten a reply, or to look up a shortcode someone shared with
        you. Not user-filtered — any authenticated bus user can read
        any submission.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "reason": "unauthenticated"})

        async with get_session() as session:
            row = await get_feedback(session, feedback_id)
        if row is None:
            return json.dumps({
                "status": "not_found",
                "id": feedback_id,
                "hint": (
                    "Shortcode not on file. Check for typos; "
                    "shortcodes are case-sensitive."
                ),
            })
        return json.dumps({"status": "ok", "feedback": to_dict(row)})

    @mcp_tool()
    async def list_feedback(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        mine_only: bool = False,
        limit: int = LIST_LIMIT_DEFAULT,
        ctx: Context = None,
    ) -> str:
        """List feedback submissions, newest first.

        Args:
            category: filter to one of "feature-request", "bug",
                "friction", "other". Omit for all categories.
            status: filter to one of "open", "in-progress", "resolved",
                "duplicate", "wontfix". Omit for all statuses.
            mine_only: when True, restrict to submissions by the
                calling user. Useful for "did any of my requests get
                answered?" workflows.
            limit: max rows to return (default 50, cap 200). If more
                rows likely exist, use filters to narrow — no pagination
                in v1.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"status": "error", "reason": "unauthenticated"})

        cat = _parse_category(category)
        if isinstance(cat, _Unknown):
            return json.dumps({
                "status": "error",
                "reason": "unknown_category",
                "given": category,
                "accepted": _VALID_CATEGORIES,
            })
        stat = _parse_status(status)
        if isinstance(stat, _Unknown):
            return json.dumps({
                "status": "error",
                "reason": "unknown_status",
                "given": status,
                "accepted": _VALID_STATUSES,
            })
        capped_limit = max(1, min(int(limit), LIST_LIMIT_MAX))

        async with get_session() as session:
            rows = await list_feedback(
                session,
                submitter_user_id=user_id if mine_only else None,
                category=cat,
                status=stat,
                limit=capped_limit,
            )
        return json.dumps({
            "status": "ok",
            "count": len(rows),
            "feedback": [to_dict(r) for r in rows],
        })
