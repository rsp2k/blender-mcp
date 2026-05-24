"""Gate G — live OAuth round-trip against a running blender-mcp server.

Requires a server running on http://127.0.0.1:8765 with
ADMIN_PASSWORD="testadmin" (matching the convention from earlier sessions).
Start it with::

    BLENDER_MCP_PORT=8765 ADMIN_PASSWORD=testadmin uv run blender-mcp &

Then::

    uv run python scripts/gate_g_oauth.py

Exits 0 on success, non-zero on any failure. Designed to be cheap
(under 2s including the deliberate 1s sleep that crosses a UTC second
boundary so the refresh test sees a fresh JWT).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from addon.auth import LoginError, login, refresh_token

SERVER_URL = "http://127.0.0.1:8765/mcp"
USERNAME = "admin"
PASSWORD = "testadmin"


def main() -> int:
    # 1. wrong password -> LoginError(401)
    try:
        login(SERVER_URL, USERNAME, "wrong-password")
        print("FAIL: expected LoginError for wrong password")
        return 1
    except LoginError as e:
        assert e.status_code == 401, e
        print(f"  1. bad-password -> LoginError({e.status_code}) OK")

    # 2. correct credentials -> JWT with user payload
    result = login(SERVER_URL, USERNAME, PASSWORD)
    assert result["user"]["username"] == USERNAME
    assert len(result["access_token"]) > 100
    print(f"  2. good-creds -> JWT(len={len(result['access_token'])}) OK")

    # 3. URL stripping handles bare base URL (no /mcp suffix)
    result2 = login("http://127.0.0.1:8765", USERNAME, PASSWORD)
    assert len(result2["access_token"]) > 100
    print("  3. URL stripping (no /mcp) -> JWT OK")

    # 4. refresh — sleep past 1s boundary to guarantee a different exp
    time.sleep(1.1)
    refresh_result = refresh_token(SERVER_URL, result["refresh_token"])
    assert "access_token" in refresh_result
    assert len(refresh_result["access_token"]) > 100
    print(f"  4. refresh -> JWT(len={len(refresh_result['access_token'])}) OK")

    # 5. refresh with a malformed token -> LoginError(401)
    try:
        refresh_token(SERVER_URL, "not.a.jwt")
        print("FAIL: expected LoginError for bad refresh token")
        return 1
    except LoginError as e:
        assert e.status_code == 401, e
        print(f"  5. refresh-bad-token -> LoginError({e.status_code}) OK")

    print("\nGate G: PASS — 5/5 OAuth scenarios green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
