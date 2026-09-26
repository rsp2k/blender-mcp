"""Blender main-thread queue drainer.

`bpy.app.timers.register` calls into `drain_queue` ~100ms (configurable),
which pops the highest-priority queued job and executes it on Blender's
main thread. Returning a float reschedules; returning None unregisters
the timer.

Script execution runs synchronously here. The result/error is reported
back to the bus via `job_reporter.submit_job_update`, which marshals the
reply onto the asyncio loop on the worker thread.
"""

from __future__ import annotations

import heapq
import io
import time
import traceback
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Optional

import bmesh
import bpy
import mathutils

from .. import state
from .job_reporter import make_progress_reporter, submit_job_update

if TYPE_CHECKING:
    from .bus_client import BlenderMCPClient


DRAIN_INTERVAL_S = 0.1


def drain_queue(client: "BlenderMCPClient") -> Optional[float]:
    """Pop the next job (if any) and dispatch it. Reschedules the timer.

    Returns:
        Next interval (seconds) to wait, or None to unregister.
    """
    if not client.running:
        client._timer_registered = False
        return None  # unregister timer

    with client.queue_lock:
        if not client.job_queue:
            return DRAIN_INTERVAL_S
        _, _, log_data = heapq.heappop(client.job_queue)

    # Server wire shape:
    #   {user_id, from_uuid, target_uuid, routing, payload,
    #    job_id, message_id, priority, timestamp}
    # `payload` holds the LLM-sent dict. Two message_types supported:
    #   "job_dispatch"     — {job_id, script} (pre-Phase-A, free-form exec)
    #   "command_dispatch" — {job_id, command, params} (Phase A, named handler)
    target = log_data.get("target_uuid")
    if target and target != client.client_uuid:
        return 0.0  # not for us; check again immediately

    payload = log_data.get("payload", log_data)
    msg_type = payload.get("message_type")
    job_id = payload.get("job_id")
    if not job_id:
        return 0.0

    if msg_type == "job_dispatch":
        script = payload.get("script", "")
        if not script:
            return 0.0
        submit_job_update(client, job_id, "running")
        execute_script(client, job_id, script)
    elif msg_type == "command_dispatch":
        command = payload.get("command", "")
        params = payload.get("params") or {}
        if not command:
            return 0.0
        # Lets the server tell "waiting in the queue" from "Blender is on it".
        submit_job_update(client, job_id, "running")
        execute_command(client, job_id, command, params)
    elif msg_type == "control_request":
        # Cooperative advisory lock. Auto-grants if the requester is
        # on prefs.pre_authorized_llms; otherwise stashes into state
        # for the sidebar banner to render Allow/Deny/Always-allow.
        handle_control_request(client, job_id, payload)
    elif msg_type == "extension_install_request":
        # Bus-driven extension install. Auto-installs only when BOTH
        # (a) the requester is pre-authorized AND (b) the repo URL is
        # on prefs.pre_authorized_extension_repos. A new repo always
        # prompts even for trusted LLMs, since adding a repo is a
        # separate trust decision from installing from an already-
        # trusted one.
        handle_extension_install_request(client, job_id, payload)
    # Unknown message_type silently dropped (forward-compatible: future
    # message types in the wire won't make older addons crash).

    return 0.0  # check for next immediately


def execute_script(client: "BlenderMCPClient", job_id: str, script: str) -> None:
    """Execute a dispatched script in Blender's main thread, report result."""
    client.active_jobs[job_id] = time.time()
    output = io.StringIO()
    reporter = make_progress_reporter(client, job_id)
    state._current_progress = reporter

    exec_globals = {
        "report_progress": reporter,
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        # Handler helpers (PolyHaven, Hyper3D, etc.) live on the executor.
        # The client stores a reference at construction time.
        "executor": client.executor,
        "__name__": "__blender_mcp_job__",
    }

    try:
        with redirect_stdout(output):
            exec(compile(script, f"<job_{job_id}>", "exec"), exec_globals)
        submit_job_update(
            client, job_id, "completed",
            result=output.getvalue(), error="",
        )
    except Exception as e:
        tb = traceback.format_exc()
        submit_job_update(
            client, job_id, "failed",
            result=output.getvalue(),
            error=f"{e}\n{tb}",
        )
    finally:
        client.active_jobs.pop(job_id, None)
        state._current_progress = None


def execute_command(
    client: "BlenderMCPClient",
    job_id: str,
    command: str,
    params: dict,
) -> None:
    """Dispatch a named command via the executor's registry, report result.

    The server-side dispatch_component sends this shape instead of free-form
    scripts so MCP tools can present a typed surface (e.g. blender_get_scene_info)
    without script-injection hazards. The executor's @command-registered
    method runs on Blender's main thread (we're called from drain_queue
    which IS the main thread, via bpy.app.timers), captures any stdout,
    and the result/error round-trip back to the server via submit_job_update
    exactly like execute_script does.

    Unknown commands or commands gated off by AddonPreferences come back
    as status="failed" with the addon-side error message intact — server
    tier-3 wrappers can recognize "gated_off" hints and surface them.
    """
    client.active_jobs[job_id] = time.time()
    output = io.StringIO()
    # execute_code exposes this to the job's code as report_progress().
    state._current_progress = make_progress_reporter(client, job_id)

    try:
        with redirect_stdout(output):
            result = client.executor.execute_command(
                {"type": command, "params": params}
            )
        # execute_command already wraps its return in {"status", "result"}
        # or {"status", "message"}; collapse that into our wire shape.
        if isinstance(result, dict) and result.get("status") == "error":
            submit_job_update(
                client, job_id, "failed",
                result=output.getvalue(),
                error=str(result.get("message", "")),
            )
        else:
            submit_job_update(
                client, job_id, "completed",
                # Serialize the inner result; the dispatch handler returns
                # arbitrary JSON-able shapes.
                result=_safe_json(result.get("result") if isinstance(result, dict) else result),
                error="",
            )
    except Exception as e:
        tb = traceback.format_exc()
        submit_job_update(
            client, job_id, "failed",
            result=output.getvalue(),
            error=f"{e}\n{tb}",
        )
    finally:
        client.active_jobs.pop(job_id, None)
        state._current_progress = None


def _pre_authorized(prefs_uuids: str, requester_uuid: str) -> bool:
    """True iff requester_uuid appears in the comma-separated allow-list."""
    if not requester_uuid:
        return False
    return requester_uuid in {u.strip() for u in prefs_uuids.split(",") if u.strip()}


def _get_prefs():
    """Local prefs lookup that tolerates being called before register.

    Uses ADDON_PACKAGE_NAME so the same code works whether the addon
    was installed the legacy way (package "addon") or as a Blender
    extension (package "bl_ext.<repo>.blender_mcp").
    """
    from ..preferences import ADDON_PACKAGE_NAME
    try:
        return bpy.context.preferences.addons[ADDON_PACKAGE_NAME].preferences
    except (KeyError, AttributeError):
        return None


def _tag_redraw() -> None:
    """Kick the sidebar to redraw so a pending-request banner appears fast."""
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except (AttributeError, RuntimeError):
        # bpy.context isn't always populated (edge cases during startup).
        pass


def handle_control_request(
    client: "BlenderMCPClient",
    job_id: str,
    payload: dict,
) -> None:
    """Process an incoming control_request. Grant instantly if pre-authed,
    otherwise store as pending for the sidebar to prompt the user."""
    requester_uuid = payload.get("requester_uuid") or ""
    requester_label = payload.get("requester_label")
    reason = payload.get("reason") or "(no reason given)"
    duration_s = float(payload.get("duration_s") or 60.0)

    prefs = _get_prefs()
    pre_auth_list = getattr(prefs, "pre_authorized_llms", "") if prefs else ""

    if _pre_authorized(pre_auth_list, requester_uuid):
        # Silent auto-grant. The active-lock header still surfaces the
        # holder, so it's not truly invisible — just no click required.
        expires_at = time.time() + duration_s
        state._lock_holder_uuid = requester_uuid
        state._lock_holder_label = requester_label
        state._lock_expires_at = expires_at
        state._lock_reason = reason
        _tag_redraw()
        submit_job_update(
            client, job_id, "completed",
            result=_safe_json({
                "granted": True,
                "auto_granted": True,
                "expires_at": expires_at,
            }),
            error="",
        )
        return

    # Prompt path: store pending; operators pick this up on click.
    state._pending_control_request = {
        "job_id": job_id,
        "requester_uuid": requester_uuid,
        "requester_label": requester_label,
        "reason": reason,
        "duration_s": duration_s,
    }
    _tag_redraw()
    # NO submit_job_update here — the reply happens when the user clicks
    # Allow / Deny / Always-allow (see BLENDERMCP_OT_GrantControl etc.
    # in ui/operators.py).


def _pre_auth_repo(prefs_urls: str, repo_url: str) -> bool:
    """True iff repo_url is in the comma-separated allowlist."""
    if not repo_url:
        return False
    allowlist = {u.strip().rstrip("/") for u in prefs_urls.split(",") if u.strip()}
    return repo_url.strip().rstrip("/") in allowlist


def _find_registered_repo(remote_url: str):
    """Return (repo_index, repo) for the first extension repo whose
    remote_url matches, or (None, None) if not registered.

    Blender exposes the repo list at ``bpy.context.preferences.extensions.repos``
    on 4.2+; older Blenders don't have this at all and this function
    just returns (None, None).
    """
    try:
        repos = bpy.context.preferences.extensions.repos
    except AttributeError:
        return None, None
    needle = (remote_url or "").strip().rstrip("/")
    for idx, repo in enumerate(repos):
        remote = (getattr(repo, "remote_url", "") or "").strip().rstrip("/")
        if remote == needle:
            return idx, repo
    return None, None


def _perform_extension_install(
    repo_url: str,
    package_id: str,
    repo_name: str,
) -> dict:
    """Actually invoke the Blender extensions manager operators.

    Runs on the drainer (main) thread so bpy.ops calls are legal.
    Returns a JSON-safe dict; the caller feeds it into submit_job_update.
    Any exception path lands as ``{"installed": False, "error": "..."}``
    so the LLM sees a structured failure instead of an addon-side raise.
    """
    if bpy.app.background:
        return {"installed": False, "error": "no_ui_in_background"}
    try:
        # Add the repo if not already registered.
        idx, repo = _find_registered_repo(repo_url)
        added_repo = False
        if idx is None:
            # remote_url is the reliable kwarg name across 4.2+; name
            # falls back to the URL if not supplied (Blender assigns
            # something reasonable).
            bpy.ops.extensions.repo_add(remote_url=repo_url, name=repo_name or repo_url)
            idx, repo = _find_registered_repo(repo_url)
            if idx is None:
                return {"installed": False, "error": "repo_add_did_not_register"}
            added_repo = True
            # Sync so the index is available before install.
            bpy.ops.extensions.repo_sync(repo_index=idx)

        bpy.ops.extensions.package_install(repo_index=idx, pkg_id=package_id)
        # package_install returns as soon as the download is queued —
        # actual extraction runs on Blender's background thread and
        # completes later. Honest wire: report queued, and hint that
        # list_installed_extensions is the way to confirm completion.
        return {
            "install_initiated": True,
            "repo_index": idx,
            "repo_added": added_repo,
            "hint": (
                "Blender's extensions manager runs the download + "
                "extraction on a background thread. Poll "
                "blender_list_installed_extensions to confirm the "
                "package appears (usually a few seconds; can be longer "
                "for wheel-bearing extensions like blender_mcp)."
            ),
        }
    except Exception as e:
        return {"install_initiated": False, "error": f"{type(e).__name__}: {e}"}


def handle_extension_install_request(
    client: "BlenderMCPClient",
    job_id: str,
    payload: dict,
) -> None:
    """Route an incoming extension_install_request through the consent
    check + install path.

    Auto-install requires BOTH: the requester UUID on the pre-authorized
    LLM list, AND the repo URL already on the pre-authorized repo list.
    A new-repo install always prompts, so adding a repo is opt-in every
    time even for trusted LLMs.
    """
    requester_uuid = payload.get("requester_uuid") or ""
    requester_label = payload.get("requester_label")
    repo_url = payload.get("repo_url") or ""
    repo_name = payload.get("repo_name") or ""
    package_id = payload.get("package_id") or ""
    reason = payload.get("reason") or "(no reason given)"

    if not repo_url or not package_id:
        submit_job_update(
            client, job_id, "completed",
            result=_safe_json({"installed": False, "error": "missing_repo_url_or_package_id"}),
            error="",
        )
        return

    prefs = _get_prefs()
    pre_auth_llms = getattr(prefs, "pre_authorized_llms", "") if prefs else ""
    pre_auth_repos = getattr(prefs, "pre_authorized_extension_repos", "") if prefs else ""

    llm_trusted = _pre_authorized(pre_auth_llms, requester_uuid)
    repo_trusted = _pre_auth_repo(pre_auth_repos, repo_url)

    # Determine whether this would ADD a new repo — used both for the
    # auto-vs-prompt decision AND for the sidebar banner wording.
    idx, _repo = _find_registered_repo(repo_url)
    would_add_repo = idx is None

    # Fast path: LLM + repo both trusted AND we're not adding a repo.
    # Adding a repo is always a fresh trust decision, so it prompts
    # even when both trust checks pass.
    if llm_trusted and repo_trusted and not would_add_repo:
        result = _perform_extension_install(repo_url, package_id, repo_name)
        result["auto_granted"] = True
        submit_job_update(
            client, job_id, "completed",
            result=_safe_json(result),
            error="",
        )
        return

    # Prompt path — banner draws until the user clicks.
    state._pending_extension_request = {
        "job_id": job_id,
        "requester_uuid": requester_uuid,
        "requester_label": requester_label,
        "repo_url": repo_url,
        "repo_name": repo_name,
        "package_id": package_id,
        "reason": reason,
        "new_repo": would_add_repo,
    }
    _tag_redraw()


def _safe_json(value) -> str:
    """Best-effort JSON encoding for handler return values.

    Handlers can return dicts, lists, primitives — but also bpy objects
    that aren't JSON-serializable. Fall back to repr() for those rather
    than failing the whole job.
    """
    import json
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
