"""Plain-words feedback for a failed Login, shown in the panel.

The OAuth flow runs on a worker thread and its result arrives later on a
timer, so the operator that started it can no longer report anything. The
timer hands the raw error to record_login_failure(), which stores a short
human message on state._login_error for the panel to draw near the Login
button, and keeps the raw text in the console.

No bpy import, so the mapping and the state change are unit-testable.
"""

from __future__ import annotations

import re

from .. import package_health, state

MAX_MESSAGE_CHARS = 120

TIMEOUT_MESSAGE = "Login timed out waiting for the browser. Click Login to try again."
CANCELLED_MESSAGE = "Login was cancelled in the browser."
NETWORK_MESSAGE = (
    "Could not reach the BlenderMCP server. "
    "Check your internet connection and try again."
)
TLS_MESSAGE = "Could not make a secure connection to the BlenderMCP server."
STATE_MISMATCH_MESSAGE = (
    "The browser answered a different login attempt. Click Login to try again."
)
UNKNOWN_MESSAGE = "Login failed. The system console has the details."

_NETWORK_MARKERS = (
    "max retries exceeded",
    "failed to establish a new connection",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "getaddrinfo failed",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "connection aborted",
    "connection reset",
    "read timed out",
    "connect timeout",
    "connecttimeout",
    "timed out",
)
_TLS_MARKERS = ("certificate verify failed", "sslerror", "ssl:")
_CANCEL_MARKERS = ("access_denied", "cancel", "denied")


def _first_line(raw: str) -> str:
    text = raw.strip().removeprefix("Unexpected: ")
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) > MAX_MESSAGE_CHARS:
        line = line[: MAX_MESSAGE_CHARS - 3].rstrip() + "..."
    return line


def friendly_login_error(raw: str | None) -> str:
    """Map a raw OAuth failure to a sentence a user can act on.

    Covers the common kinds (browser timeout, cancelled in the browser,
    server unreachable, TLS, a server-side HTTP error); anything else
    falls back to the first line of the error, trimmed.
    """
    text = (raw or "").strip()
    if not text:
        return UNKNOWN_MESSAGE
    low = text.lower()

    if "no callback received within" in low:
        return TIMEOUT_MESSAGE
    if low.startswith("authorization failed:"):
        detail = text[len("authorization failed:"):].strip().rstrip(":").strip()
        if any(m in detail.lower() for m in _CANCEL_MARKERS):
            return CANCELLED_MESSAGE
        return _first_line(f"The login page reported an error: {detail}")
    if "state mismatch" in low:
        return STATE_MISMATCH_MESSAGE
    status = re.search(r"(?:dcr|token exchange) failed: http (\d{3})", low)
    if status:
        return f"The server refused the login (HTTP {status.group(1)}). Try again in a minute."
    if any(m in low for m in _TLS_MARKERS):
        return TLS_MESSAGE
    if any(m in low for m in _NETWORK_MARKERS):
        return NETWORK_MESSAGE
    return _first_line(text) or UNKNOWN_MESSAGE


def record_login_failure(raw: str | None) -> str:
    """Store the panel message for a failed login and return it.

    A failure that looks like a vanished package file (the certifi CA
    bundle, typically) is confirmed against the disk; if the packages
    really are gone, the panel gets the restart message instead.
    """
    print(f"[BlenderMCP] OAuth login failed: {raw}")
    message = None
    if (raw and package_health.is_missing_file_error(Exception(raw))
            and not package_health.update_state().ok):
        message = package_health.RESTART_MESSAGE
    if message is None:
        message = friendly_login_error(raw)
    state._login_error = message
    return message


def refuse_if_packages_missing() -> str | None:
    """Pre-Login check. Returns the restart message (and sets it as the
    panel's login error) when bundled packages are gone, else None."""
    if package_health.update_state().ok:
        return None
    state._login_error = package_health.RESTART_MESSAGE
    return package_health.RESTART_MESSAGE


def clear_login_error() -> None:
    state._login_error = None
