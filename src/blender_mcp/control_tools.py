"""Cooperative control-lock MCP tools.

Three tools that let an LLM ask a Blender addon for a time-boxed
"I'm about to make invasive edits" advisory lock. The addon shows the
user a compact banner (Allow / Deny / Always-allow), the LLM waits for
the answer, and the granted lock is tracked server-side on the target
``ClientInfo`` so any other LLM on the same bus can see who holds it.

Advisory in v1: existing dispatch tools do NOT refuse when someone
else holds the lock. Well-behaved LLMs check ``get_control_state`` +
call ``request_control`` before ``execute_code`` / ``job_dispatch``.
Pre-authorization on the addon side handles the "just work" case for
trusted requesters; the "Take back" button on the addon is the human's
escape hatch.

Wire shape mirrors dispatch_component: request_control goes out as a
new ``control_request`` message_type payload, the addon replies via
the same ``blender_job_update`` pipeline as dispatch replies, and the
result is a JSON-encoded dict this tool decodes into the response.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid as _uuid_mod
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _pending_jobs, _resolve_user_id, resolve_bus
from .client_role import require_role
from .job_waiter import job_waiter
from .message_router import Priority


# How long the LLM waits for the human to click Allow/Deny before we
# time the request out. Long enough to walk back to the desk, short
# enough that a genuinely-AFK user doesn't hold up the LLM indefinitely.
USER_RESPONSE_TIMEOUT_S = 45.0

# Cap on requested lock duration. Prevents an LLM from asking for an
# hour and forgetting to release. Auto-release + Take-back cover the
# tail even if the LLM sets the max.
MAX_LOCK_DURATION_S = 600.0
DEFAULT_LOCK_DURATION_S = 60.0


def _new_control_job_id() -> str:
    return f"ctl-{_uuid_mod.uuid4().hex[:12]}"


async def _dispatch_control_request(
    bus,
    bus_id_str: str,
    target_uuid: str,
    requester_uuid: str,
    requester_label: Optional[str],
    reason: str,
    duration_s: float,
) -> dict[str, Any]:
    """Send a control_request to the addon, await Allow/Deny reply.

    Returns a dict shaped like the addon's reply payload. Timeout
    yields ``{"granted": False, "reason": "user_no_response"}``.
    """
    job_id = _new_control_job_id()
    future = job_waiter.register(bus_id_str, job_id)
    _pending_jobs[job_id] = (bus_id_str, f"server-control:{bus_id_str}")

    bus.route(
        payload={
            "message_type": "control_request",
            "job_id": job_id,
            "requester_uuid": requester_uuid,
            "requester_label": requester_label,
            "reason": reason,
            "duration_s": duration_s,
        },
        from_uuid=f"server-control:{bus_id_str}",
        routing={"type": "direct", "target_uuid": target_uuid},
        priority=Priority.INFO,
        job_id=job_id,
    )

    try:
        result = await asyncio.wait_for(future, timeout=USER_RESPONSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        job_waiter.cancel(bus_id_str, job_id)
        _pending_jobs.pop(job_id, None)
        return {
            "granted": False,
            "reason": "user_no_response",
            "waited_seconds": USER_RESPONSE_TIMEOUT_S,
            "hint": (
                "The addon didn't reply within the response window. "
                "The user may be AFK, or the addon may not be connected. "
                "Try blender_get_control_state to check liveness."
            ),
        }

    # submit_job_update wire shape: {status: completed|failed, result: json_str, error: str}
    if result.get("status") != "completed":
        return {
            "granted": False,
            "reason": "addon_error",
            "error": result.get("error", ""),
        }
    try:
        return json.loads(result.get("result") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"granted": False, "reason": "addon_bad_reply"}


class BlenderControlComponent(MCPMixin):
    """Three tools for cooperative control coordination.

    All three are gated to the ``llm-client`` role: the addon itself
    doesn't call these (it responds to control_request messages instead).
    """

    @mcp_tool()
    @require_role("llm-client")
    async def request_control(
        self,
        target_uuid: str,
        reason: str,
        requester_uuid: str,
        requester_label: Optional[str] = None,
        duration_s: float = DEFAULT_LOCK_DURATION_S,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Ask a Blender instance for an advisory control lock.

        Blocks up to ~45s while the human clicks Allow/Deny (or the
        addon's pre-auth allow-list auto-grants). Returns JSON with
        ``granted`` and, on success, ``expires_at`` (unix epoch).

        Extending a lock you already hold is idempotent — call again
        with the same ``requester_uuid`` and the expiry gets bumped
        without re-prompting the user.

        Args:
            target_uuid: UUID of the Blender client to lock (from
                ``blender_list_available_clients``).
            reason: One-sentence explanation shown to the user, e.g.
                "Build a chair" or "Bake shadows for the current scene."
            requester_uuid: STABLE UUID identifying THIS LLM. Reuse
                across sessions so the user's "always allow" grant
                sticks. Not enforced (advisory system) but the addon
                trusts what you send.
            requester_label: Human-readable name shown in the addon
                banner ("Claude Desktop", "Ryan's terminal", etc.).
            duration_s: How long you want the lock. Capped at 600s;
                request again to extend before it lapses.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"granted": False, "reason": "unauthenticated"})

        duration_s = min(max(1.0, float(duration_s)), MAX_LOCK_DURATION_S)

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps({"granted": False, **resolved})

        bus = resolved["bus"]
        target = bus.get(target_uuid)
        if target is None or target.client_type != "blender":
            return json.dumps({
                "granted": False,
                "reason": "no_such_blender",
                "target_uuid": target_uuid,
                "hint": "No Blender client with that UUID on this bus.",
            })

        now = time.time()
        # Same-holder extension: idempotent, no user prompt.
        if target.lock_is_active(now) and target.control_holder_uuid == requester_uuid:
            target.control_expires_at = now + duration_s
            target.control_reason = reason
            return json.dumps({
                "granted": True,
                "extended": True,
                "expires_at": target.control_expires_at,
                "holder_uuid": requester_uuid,
            })

        # Held by someone else: reject without bothering the user.
        if target.lock_is_active(now):
            return json.dumps({
                "granted": False,
                "reason": "held_by_other",
                "holder_uuid": target.control_holder_uuid,
                "holder_label": target.control_holder_label,
                "expires_at": target.control_expires_at,
                "hint": "Poll blender_get_control_state until the lock releases, or ask the user to Take back control.",
            })

        # Clear cover — ask the addon.
        reply = await _dispatch_control_request(
            bus=bus,
            bus_id_str=str(resolved["bus_id"]),
            target_uuid=target_uuid,
            requester_uuid=requester_uuid,
            requester_label=requester_label,
            reason=reason,
            duration_s=duration_s,
        )

        if reply.get("granted"):
            # Trust the addon's expiry if it sent one (accommodates a user
            # who clicks Allow with a modified duration via a future UI),
            # otherwise use what we asked for.
            expires_at = reply.get("expires_at")
            if not isinstance(expires_at, (int, float)):
                expires_at = time.time() + duration_s
            target.control_holder_uuid = requester_uuid
            target.control_holder_label = requester_label
            target.control_expires_at = float(expires_at)
            target.control_reason = reason
            reply["expires_at"] = target.control_expires_at
            reply["holder_uuid"] = requester_uuid

        return json.dumps(reply)

    @mcp_tool()
    @require_role("llm-client")
    async def release_control(
        self,
        target_uuid: str,
        requester_uuid: str,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Voluntarily release a lock you currently hold.

        No-op if the target has no active lock or if a different LLM
        holds it (returns ``{"released": false, "reason": ...}``). The
        addon's "Take back" button releases through a separate path
        (the addon itself asks the server via its bus_client).
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"released": False, "reason": "unauthenticated"})

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps({"released": False, **resolved})

        target = resolved["bus"].get(target_uuid)
        if target is None or target.client_type != "blender":
            return json.dumps({
                "released": False,
                "reason": "no_such_blender",
                "target_uuid": target_uuid,
            })

        if not target.lock_is_active():
            return json.dumps({"released": False, "reason": "no_active_lock"})

        if target.control_holder_uuid != requester_uuid:
            return json.dumps({
                "released": False,
                "reason": "not_holder",
                "holder_uuid": target.control_holder_uuid,
            })

        target.clear_lock()
        return json.dumps({"released": True, "target_uuid": target_uuid})

    @mcp_tool()
    @require_role("addon")
    async def force_release_control(
        self,
        target_uuid: str,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Force-release a lock. Gated to the addon role.

        The addon calls this when the user clicks "Take back control"
        in the sidebar. There's no ownership check because a human at
        the keyboard trumps any LLM claim on their own Blender.

        Returns ``{"released": true, "was_held_by": <uuid|null>}``.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"released": False, "reason": "unauthenticated"})

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps({"released": False, **resolved})

        target = resolved["bus"].get(target_uuid)
        if target is None or target.client_type != "blender":
            return json.dumps({
                "released": False,
                "reason": "no_such_blender",
                "target_uuid": target_uuid,
            })

        prior_holder = target.control_holder_uuid
        target.clear_lock()
        return json.dumps({
            "released": True,
            "target_uuid": target_uuid,
            "was_held_by": prior_holder,
        })

    @mcp_tool()
    async def get_control_state(
        self,
        target_uuid: str,
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Report whether a Blender instance currently has an active lock.

        Not role-gated: anyone on the bus can poll. Returns
        ``{"has_lock": true, "holder_uuid", "holder_label", "expires_at", "reason"}``
        when a lock is active, ``{"has_lock": false}`` otherwise.
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"has_lock": False, "error": "unauthenticated"})

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps({"has_lock": False, **resolved})

        target = resolved["bus"].get(target_uuid)
        if target is None or target.client_type != "blender":
            return json.dumps({
                "has_lock": False,
                "reason": "no_such_blender",
                "target_uuid": target_uuid,
            })

        if not target.lock_is_active():
            return json.dumps({"has_lock": False, "target_uuid": target_uuid})

        return json.dumps({
            "has_lock": True,
            "target_uuid": target_uuid,
            "holder_uuid": target.control_holder_uuid,
            "holder_label": target.control_holder_label,
            "expires_at": target.control_expires_at,
            "reason": target.control_reason,
        })
