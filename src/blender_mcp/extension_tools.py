"""Bus-driven Blender extension install.

Two tools:

- ``blender_install_extension`` — dispatches an install-request to a
  target Blender addon. Addon shows an Allow / Deny / Always-allow
  banner (or auto-installs if both the LLM UUID and the repo URL are
  pre-authorized AND the repo is already registered). LLM blocks up
  to ~2 minutes while the extraction + wheel install happens.

- ``blender_list_installed_extensions`` — plain dispatch tool that
  wraps the addon's list_installed_extensions @command. Read-only,
  no consent needed. Actually lives in dispatch_component.py so it
  reuses the standard dispatch wrapper; only install_extension needs
  the custom consent-dispatch pattern.

Consent model, per the design brief:
* New repo → always prompts, even if the requester is pre-authorized
  (adding a repo is a fresh trust decision).
* Registered repo + pre-authorized LLM + pre-authorized repo URL →
  auto-install (fast path).
* Otherwise → prompt.

Enforcement of that policy lives entirely on the addon side (in
drainer.handle_extension_install_request); the server just routes the
request through the bus and waits for the reply.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid_mod
from typing import Any, Optional

from fastmcp import Context
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_tool

from .bus_tools import _pending_jobs, _resolve_user_id, resolve_bus
from .client_role import require_role
from .job_waiter import job_waiter
from .message_router import Priority


# Install can take a while: fresh repo add + sync + wheel-bearing zip
# extraction easily hits 60-90s on the 65MB self-hosted repo. Give
# the user room to click Allow AND for Blender to finish the work.
INSTALL_TIMEOUT_S = 180.0


def _new_install_job_id() -> str:
    return f"ext-{_uuid_mod.uuid4().hex[:12]}"


async def _dispatch_extension_install(
    bus,
    bus_id_str: str,
    target_uuid: str,
    requester_uuid: str,
    requester_label: Optional[str],
    repo_url: str,
    repo_name: str,
    package_id: str,
    reason: str,
) -> dict[str, Any]:
    """Send extension_install_request payload, await addon reply."""
    job_id = _new_install_job_id()
    future = job_waiter.register(bus_id_str, job_id)
    _pending_jobs[job_id] = (bus_id_str, f"server-ext:{bus_id_str}")

    bus.route(
        payload={
            "message_type": "extension_install_request",
            "job_id": job_id,
            "requester_uuid": requester_uuid,
            "requester_label": requester_label,
            "repo_url": repo_url,
            "repo_name": repo_name,
            "package_id": package_id,
            "reason": reason,
        },
        from_uuid=f"server-ext:{bus_id_str}",
        routing={"type": "direct", "target_uuid": target_uuid},
        priority=Priority.INFO,
        job_id=job_id,
    )

    try:
        result = await asyncio.wait_for(future, timeout=INSTALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        job_waiter.cancel(bus_id_str, job_id)
        _pending_jobs.pop(job_id, None)
        return {
            "installed": False,
            "reason": "timeout",
            "waited_seconds": INSTALL_TIMEOUT_S,
            "hint": (
                "No addon reply within the install window. Either the "
                "user didn't answer the consent prompt, or the install "
                "itself hung. Verify the addon is connected with "
                "blender_list_available_clients and try again."
            ),
        }

    if result.get("status") != "completed":
        return {
            "installed": False,
            "reason": "addon_error",
            "error": result.get("error", ""),
        }
    try:
        return json.loads(result.get("result") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"installed": False, "reason": "addon_bad_reply"}


class BlenderExtensionComponent(MCPMixin):
    """One tool: blender_install_extension.

    The list side is a plain dispatch tool wired into dispatch_component.py
    (its addon-side handler is a normal @command). Consent + async
    install belong in their own component because their wire shape
    differs.
    """

    @mcp_tool()
    @require_role("llm-client")
    async def install_extension(
        self,
        target_uuid: str,
        repo_url: str,
        package_id: str,
        requester_uuid: str,
        requester_label: Optional[str] = None,
        repo_name: Optional[str] = None,
        reason: str = "",
        bus_id: Optional[str] = None,
        ctx: Context = None,
    ) -> str:
        """Ask a Blender addon to install an extension via its 4.2+
        extensions manager.

        The addon may prompt the user for consent (Allow / Deny /
        Always-allow) OR auto-install if BOTH the requester's LLM UUID
        AND the repo URL are on the addon's pre-authorized lists AND
        the repo is already registered. Adding a new repo always
        prompts, even for trusted LLMs.

        Args:
            target_uuid: Blender client UUID from list_available_clients.
            repo_url: The extension repo's index.json URL, e.g.
                ``https://mcp.blender.bet/extensions/index.json``.
            package_id: The extension's manifest id, e.g. ``blender_mcp``.
            requester_uuid: STABLE UUID for this LLM. Reuse across
                sessions so the user's "always allow" grant sticks.
            requester_label: Human-readable name shown in the banner.
            repo_name: Optional friendly name for the repo when adding
                it fresh. Defaults to the URL if omitted.
            reason: One-sentence explanation shown to the user, e.g.
                "Need the polyhaven integration extension for HDRIs".

        Returns JSON with:
        - ``installed`` (bool)
        - ``auto_granted`` (bool) — True when the fast path fired
        - ``repo_added`` (bool) — True when we had to add the repo first
        - ``restart_required`` (bool) — extension needs Blender restart
        - ``error`` (str) — populated when installed=False
        """
        user_id = _resolve_user_id(ctx)
        if not user_id:
            return json.dumps({"installed": False, "reason": "unauthenticated"})

        if not (repo_url or "").strip() or not (package_id or "").strip():
            return json.dumps({
                "installed": False,
                "reason": "repo_url_and_package_id_required",
            })

        resolved = await resolve_bus(user_id, bus_id)
        if not resolved["ok"]:
            return json.dumps({"installed": False, **resolved})

        bus = resolved["bus"]
        target = bus.get(target_uuid)
        if target is None or target.client_type != "blender":
            return json.dumps({
                "installed": False,
                "reason": "no_such_blender",
                "target_uuid": target_uuid,
            })

        reply = await _dispatch_extension_install(
            bus=bus,
            bus_id_str=str(resolved["bus_id"]),
            target_uuid=target_uuid,
            requester_uuid=requester_uuid,
            requester_label=requester_label,
            repo_url=repo_url.strip(),
            repo_name=(repo_name or "").strip(),
            package_id=package_id.strip(),
            reason=reason,
        )
        return json.dumps(reply)
