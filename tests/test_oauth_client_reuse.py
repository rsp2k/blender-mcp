"""DCR reuse: oauth_login keeps a stored client_id unless the server rejects it."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

requests = pytest.importorskip("requests")

_PATH = Path(__file__).resolve().parent.parent / "addon" / "auth" / "oauth_pkce.py"


def _load():
    # Loaded standalone: the addon package imports bpy at package level.
    pkg = types.ModuleType("addon_stub")
    pkg.__path__ = []
    sys.modules.setdefault("addon_stub", pkg)
    spec = importlib.util.spec_from_file_location("addon_stub.oauth_pkce", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oauth = _load()


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


@pytest.mark.parametrize(
    "resp, expected",
    [
        (_Resp(302), True),
        (_Resp(400, '{"error":"invalid_request","error_description":"Client ID \'x\' not found"}'), False),
        (_Resp(401, '{"error":"invalid_client"}'), False),
        # Verbatim shape from mcp.blender.bet (FastMCP proxy rewrite).
        (_Resp(400, '{"error":"invalid_request","error_description":"Client ID \'0\' is not '
                    'registered with this server. MCP clients should automatically re-register",'
                    '"registration_endpoint":"https://mcp.blender.bet/register"}'), False),
        (_Resp(400, '{"error":"invalid_request","error_description":"missing code_challenge"}'), None),
        (_Resp(500, "boom"), None),
    ],
)
def test_preflight_classification(resp, expected):
    with mock.patch.object(oauth.requests, "get", return_value=resp):
        assert oauth._client_is_registered("https://s", "cid", "http://127.0.0.1:1/callback") is expected


def test_preflight_network_error_is_inconclusive():
    with mock.patch.object(oauth.requests, "get", side_effect=requests.ConnectionError()):
        assert oauth._client_is_registered("https://s", "cid", "http://127.0.0.1:1/callback") is None


def _run_login(stored, preflight):
    """Drive oauth_login up to client selection, then stop before the browser."""
    class _Stop(Exception):
        pass

    with mock.patch.object(oauth, "_client_is_registered", return_value=preflight) as pre, \
         mock.patch.object(oauth, "_register_client", return_value={"client_id": "new"}) as reg, \
         mock.patch.object(oauth, "_gen_pkce", side_effect=_Stop):
        with pytest.raises(_Stop):
            oauth.oauth_login("https://s", client_id=stored, open_browser=False, timeout=0)
    return pre, reg


def test_reuses_known_client():
    pre, reg = _run_login("old", True)
    pre.assert_called_once()
    reg.assert_not_called()


def test_keeps_client_when_preflight_inconclusive():
    _pre, reg = _run_login("old", None)
    reg.assert_not_called()


def test_registers_when_server_forgot_client():
    _pre, reg = _run_login("old", False)
    reg.assert_called_once()


def test_registers_when_nothing_stored():
    pre, reg = _run_login(None, True)
    pre.assert_not_called()
    reg.assert_called_once()
