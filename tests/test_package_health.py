"""Bundled packages vanishing mid-session (addon/package_health.py), the
plain-words Login failure message (addon/auth/login_feedback.py), and the
connection supervisor holding off while packages are missing.

Incident 2026-10-10: Blender's shared extension site-packages was cleared
while Blender ran; certifi's cacert.pem went with it, so every reconnect
and Login failed with "[Errno 2] No such file or directory" and the Login
button looked like it did nothing.
"""

import sys
import types
from types import SimpleNamespace

import pytest

# addon.client's package init imports the Blender runtime; the code under
# test here doesn't use it, so stand in empty modules outside Blender.
for _name in ("bpy", "bmesh", "mathutils"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

import addon.auth
from addon import connection, package_health, state
from addon.activity_format import wrap_message
from addon.auth import login_feedback
from addon.auth.login_feedback import friendly_login_error
from addon.client.bus_client import BlenderMCPClient
from addon.package_health import PackageHealth

CA_ERROR = (
    "Unexpected: Could not find a suitable TLS CA certificate bundle, invalid path: "
    "/home/u/.config/blender/5.2/extensions/.local/lib/python3.14/site-packages/"
    "certifi/cacert.pem"
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(state, "_package_problem", None)
    monkeypatch.setattr(state, "_login_error", None)
    monkeypatch.setattr(state, "_auth_in_progress", False)


def fake_certifi(monkeypatch, where):
    mod = types.ModuleType("certifi")
    mod.where = lambda: str(where)
    monkeypatch.setitem(sys.modules, "certifi", mod)
    return mod


@pytest.fixture
def certifi_ok(monkeypatch, tmp_path):
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    fake_certifi(monkeypatch, bundle)
    return bundle


@pytest.fixture
def certifi_gone(monkeypatch, tmp_path):
    missing = tmp_path / "site-packages" / "certifi" / "cacert.pem"
    fake_certifi(monkeypatch, missing)
    return missing


# ---- check() ------------------------------------------------------------------

def test_check_ok_when_bundle_exists(certifi_ok):
    assert package_health.check() == PackageHealth(True)


def test_check_reports_missing_bundle_path(certifi_gone):
    health = package_health.check()
    assert not health.ok
    assert health.missing_path == str(certifi_gone)


def test_check_reports_failed_import(monkeypatch):
    # None in sys.modules makes `import certifi` raise ImportError.
    monkeypatch.setitem(sys.modules, "certifi", None)
    health = package_health.check()
    assert not health.ok and health.missing_path is None
    assert "import failed" in health.detail


def test_check_reports_imported_package_whose_files_vanished(monkeypatch, certifi_ok, tmp_path):
    gone = types.ModuleType("httpx")
    gone.__file__ = str(tmp_path / "httpx" / "__init__.py")
    monkeypatch.setitem(sys.modules, "httpx", gone)
    health = package_health.check()
    assert not health.ok and health.missing_path == gone.__file__


def test_check_ignores_modules_without_a_real_file(monkeypatch, certifi_ok):
    monkeypatch.setitem(sys.modules, "fastmcp", types.ModuleType("fastmcp"))
    assert package_health.check().ok


def test_update_state_records_and_prints_once(certifi_gone, monkeypatch, tmp_path, capsys):
    assert not package_health.update_state().ok
    assert state._package_problem == str(certifi_gone)
    assert package_health.packages_missing()
    package_health.update_state()
    out = capsys.readouterr().out
    assert out.count("Bundled package files missing") == 1
    assert str(certifi_gone) in out

    certifi_gone.parent.mkdir(parents=True)
    certifi_gone.write_text("x")
    assert package_health.update_state().ok
    assert state._package_problem is None
    assert "back on disk" in capsys.readouterr().out


# ---- is_missing_file_error ----------------------------------------------------

def test_missing_file_error_direct_and_wrapped():
    assert package_health.is_missing_file_error(FileNotFoundError(2, "No such file or directory"))
    try:
        try:
            raise FileNotFoundError(2, "No such file or directory")
        except FileNotFoundError as inner:
            raise RuntimeError("Client failed to connect: [Errno 2] No such file or directory") from inner
    except RuntimeError as outer:
        assert package_health.is_missing_file_error(outer)


def test_missing_file_error_ca_text_and_groups():
    assert package_health.is_missing_file_error(OSError(CA_ERROR))
    group = ExceptionGroup("tg", [ValueError("x"), FileNotFoundError("cacert.pem")])
    assert package_health.is_missing_file_error(RuntimeError("wrapped")) is False
    assert package_health.is_missing_file_error(group)


def test_unrelated_errors_are_not_missing_files():
    assert not package_health.is_missing_file_error(ConnectionError("connect timed out after 15s"))
    # Errno -2 is DNS (Name or service not known), not a missing file.
    assert not package_health.is_missing_file_error(
        OSError("[Errno -2] Name or service not known"))


# ---- friendly_login_error -----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("No callback received within 300.0s. Did the browser open?",
     login_feedback.TIMEOUT_MESSAGE),
    ("Authorization failed: access_denied: The user cancelled",
     login_feedback.CANCELLED_MESSAGE),
    (("Unexpected: HTTPSConnectionPool(host='mcp.blender.bet', port=443): Max retries "
      "exceeded with url: /register (Caused by NewConnectionError('Failed to establish "
      "a new connection: [Errno -2] Name or service not known'))"),
     login_feedback.NETWORK_MESSAGE),
    ("Unexpected: HTTPSConnectionPool(host='x', port=443): Read timed out. (read timeout=10.0)",
     login_feedback.NETWORK_MESSAGE),
    (("Unexpected: HTTPSConnectionPool(host='x', port=443): Max retries exceeded "
      "(Caused by SSLError(SSLCertVerificationError(1, 'certificate verify failed')))"),
     login_feedback.TLS_MESSAGE),
    ("State mismatch \u2014 possible CSRF. Aborting.", login_feedback.STATE_MISMATCH_MESSAGE),
    ("DCR failed: HTTP 502 \u2014 <html>bad gateway</html>",
     "The server refused the login (HTTP 502). Try again in a minute."),
    ("Token exchange failed: HTTP 400 \u2014 {}",
     "The server refused the login (HTTP 400). Try again in a minute."),
    ("", login_feedback.UNKNOWN_MESSAGE),
    (None, login_feedback.UNKNOWN_MESSAGE),
])
def test_friendly_login_error_maps_common_kinds(raw, expected):
    assert friendly_login_error(raw) == expected


def test_friendly_login_error_other_authorization_error_names_it():
    msg = friendly_login_error("Authorization failed: server_error: upstream down")
    assert msg == "The login page reported an error: server_error: upstream down"


def test_friendly_login_error_falls_back_to_trimmed_first_line():
    raw = "Unexpected: KeyError 'client_id'\nTraceback (most recent call last):\n  ..."
    assert friendly_login_error(raw) == "KeyError 'client_id'"
    long = "Unexpected: " + "x" * 400
    msg = friendly_login_error(long)
    assert len(msg) == login_feedback.MAX_MESSAGE_CHARS and msg.endswith("...")


def test_ui_strings_have_no_em_dash():
    strings = [package_health.RESTART_MESSAGE, package_health.SHORT_MESSAGE,
               login_feedback.TIMEOUT_MESSAGE, login_feedback.CANCELLED_MESSAGE,
               login_feedback.NETWORK_MESSAGE, login_feedback.TLS_MESSAGE,
               login_feedback.STATE_MISMATCH_MESSAGE, login_feedback.UNKNOWN_MESSAGE]
    assert not any("\u2014" in s for s in strings)


# ---- record_login_failure / refuse_if_packages_missing -------------------------

def test_record_login_failure_sets_panel_message_and_prints(certifi_ok, capsys):
    raw = "No callback received within 300.0s. Did the browser open?"
    msg = login_feedback.record_login_failure(raw)
    assert msg == login_feedback.TIMEOUT_MESSAGE
    assert state._login_error == login_feedback.TIMEOUT_MESSAGE
    assert f"OAuth login failed: {raw}" in capsys.readouterr().out


def test_record_login_failure_ca_error_with_packages_gone_says_restart(certifi_gone):
    login_feedback.record_login_failure(CA_ERROR)
    assert state._login_error == package_health.RESTART_MESSAGE
    assert state._package_problem == str(certifi_gone)


def test_record_login_failure_ca_error_but_files_present_shows_the_error(certifi_ok):
    login_feedback.record_login_failure(CA_ERROR)
    assert state._login_error.startswith("Could not find a suitable TLS CA certificate bundle")
    assert state._package_problem is None


def test_refuse_if_packages_missing(certifi_ok, monkeypatch, tmp_path):
    assert login_feedback.refuse_if_packages_missing() is None
    assert state._login_error is None
    fake_certifi(monkeypatch, tmp_path / "nope" / "cacert.pem")
    assert login_feedback.refuse_if_packages_missing() == package_health.RESTART_MESSAGE
    assert state._login_error == package_health.RESTART_MESSAGE
    login_feedback.clear_login_error()
    assert state._login_error is None


def test_wrap_message_fits_narrow_and_wide_panels():
    narrow = wrap_message(package_health.RESTART_MESSAGE, 260)
    wide = wrap_message(package_health.RESTART_MESSAGE, 1200)
    assert len(narrow) > len(wide) >= 1
    assert " ".join(narrow) == package_health.RESTART_MESSAGE


# ---- connection supervisor ----------------------------------------------------

def test_decide_holds_off_while_packages_missing():
    args = {"armed": True, "has_token": True, "auth_in_progress": False, "alive": False,
            "now": 100.0, "next_attempt_at": 0.0}
    assert connection.decide(**args) == connection.START
    assert connection.decide(**args, packages_ok=False) == connection.PACKAGES_MISSING
    assert connection.decide(**{**args, "armed": False}, packages_ok=False) == connection.DISARMED


@pytest.fixture
def supervisor_env(monkeypatch):
    prefs = SimpleNamespace(auto_connect=True, jwt_token="tok")
    monkeypatch.setitem(sys.modules, "addon.preferences",
                        SimpleNamespace(get_prefs=lambda *a: prefs))
    monkeypatch.setattr(state, "_client", None)
    monkeypatch.setattr(state, "_identity", None)
    starts = []
    monkeypatch.setattr(connection, "start_client", lambda: starts.append(1) or (False, "x"))
    monkeypatch.setattr(connection, "request_ui_redraw", lambda: None)
    return SimpleNamespace(sup=connection.ConnectionSupervisor(), starts=starts)


def test_supervisor_does_not_start_clients_while_packages_missing(supervisor_env, certifi_gone):
    env = supervisor_env
    for _ in range(3):
        env.sup._evaluate()
    assert env.starts == []
    assert env.sup._last_decision == connection.PACKAGES_MISSING
    assert state._package_problem == str(certifi_gone)

    # Files come back (another Blender reinstalled them): resume at once.
    certifi_gone.parent.mkdir(parents=True)
    certifi_gone.write_text("x")
    env.sup.consecutive_failures, env.sup.next_attempt_at = 4, 1e12
    env.sup._evaluate()
    assert env.starts == [1]
    assert state._package_problem is None


# ---- bus client token refresh ---------------------------------------------------

def refresh_with(monkeypatch, exc):
    import asyncio

    def fail(*_a, **_k):
        raise exc

    monkeypatch.setattr(addon.auth, "refresh_oauth_token", fail)
    monkeypatch.setitem(sys.modules, "addon.preferences",
                        SimpleNamespace(get_prefs=lambda: SimpleNamespace(oauth_client_id="cid")))
    client = SimpleNamespace(server_url="https://mcp.example.com", refresh_token="r",
                             last_error=None)
    ok = asyncio.run(BlenderMCPClient._do_refresh_once(client))
    return ok, client


def test_refresh_crash_from_missing_ca_bundle_is_recorded(monkeypatch, certifi_gone):
    ok, client = refresh_with(monkeypatch, OSError(CA_ERROR))
    assert ok is False
    assert client.last_error == package_health.SHORT_MESSAGE
    assert package_health.packages_missing()


def test_refresh_network_crash_stays_transient(monkeypatch, certifi_ok):
    ok, client = refresh_with(monkeypatch, ConnectionError("connection reset by peer"))
    assert ok is False
    assert client.last_error.startswith("JWT refresh crashed")
    assert not package_health.packages_missing()


# ---- the Login operator, end to end with a stub bpy ---------------------------

class _Any:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __iter__(self):
        return iter(())


@pytest.fixture
def operators(monkeypatch):
    timers = []
    bpy = types.ModuleType("bpy")
    bpy.types = SimpleNamespace(Operator=object, Panel=object, AddonPreferences=object,
                                PropertyGroup=object, UIList=object, Menu=object)
    props = types.ModuleType("bpy.props")
    for name in ("StringProperty", "BoolProperty", "IntProperty", "EnumProperty",
                 "FloatProperty", "CollectionProperty", "PointerProperty"):
        setattr(props, name, lambda *a, **k: None)
    bpy.props = props
    bpy.app = SimpleNamespace(timers=SimpleNamespace(register=lambda fn, **k: timers.append(fn)),
                              driver_namespace={})
    bpy.context = _Any()
    before = set(sys.modules)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    for name in ("bmesh", "mathutils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for name in [m for m in sys.modules if m == "addon.ui" or m.startswith("addon.ui.")]:
        monkeypatch.delitem(sys.modules, name)
    import addon.ui.operators as ops

    class SyncThread:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(ops.threading, "Thread", SyncThread)
    monkeypatch.setattr(ops, "get_prefs", lambda *a: SimpleNamespace(oauth_client_id=""))
    monkeypatch.setattr(ops, "get_server_base_url", lambda prefs: "https://mcp.example.com")
    yield SimpleNamespace(ops=ops, timers=timers)
    # Modules imported against the stub bpy must not leak into other tests.
    for name in set(sys.modules) - before:
        if name.startswith("addon."):
            sys.modules.pop(name, None)


def run_login(ops, monkeypatch, exc):
    def fail(*_a, **_k):
        raise exc

    monkeypatch.setattr(ops, "oauth_login", fail)
    op = ops.BLENDERMCP_OT_OAuthLogin()
    reports = []
    op.report = lambda kind, text: reports.append((kind, text))
    return op.execute(None), reports


def test_login_failure_reaches_the_panel(operators, monkeypatch, certifi_ok):
    ops = operators.ops
    state._login_error = "an older failure"
    result, _ = run_login(ops, monkeypatch, ops.OAuthError(
        "No callback received within 300.0s. Did the browser open?"))
    assert result == {'FINISHED'}
    assert state._login_error is None  # cleared by the click
    (poll,) = operators.timers
    assert poll() is None
    assert state._auth_in_progress is False
    assert state._login_error == login_feedback.TIMEOUT_MESSAGE


def test_login_unexpected_ca_error_with_packages_gone_says_restart(operators, monkeypatch, tmp_path,
                                                                 certifi_ok):
    ops = operators.ops
    result, _ = run_login(ops, monkeypatch, OSError(CA_ERROR))
    assert result == {'FINISHED'}
    fake_certifi(monkeypatch, tmp_path / "gone" / "cacert.pem")  # vanished meanwhile
    (poll,) = operators.timers
    poll()
    assert state._login_error == package_health.RESTART_MESSAGE


def test_login_refused_before_browser_when_packages_missing(operators, monkeypatch, certifi_gone):
    ops = operators.ops
    called = []
    monkeypatch.setattr(ops, "oauth_login", lambda *a, **k: called.append(1))
    op = ops.BLENDERMCP_OT_OAuthLogin()
    reports = []
    op.report = lambda kind, text: reports.append((kind, text))
    assert op.execute(None) == {'CANCELLED'}
    assert called == [] and operators.timers == []
    assert state._login_error == package_health.RESTART_MESSAGE
    assert reports == [({'ERROR'}, package_health.RESTART_MESSAGE)]


def test_chat_names_the_restart_when_packages_are_missing(monkeypatch):
    from addon.chat import client as chat_client
    monkeypatch.setattr(state, "_package_problem", "CA bundle missing")
    assert chat_client._not_connected() == package_health.CHAT_MESSAGE
    monkeypatch.setattr(state, "_package_problem", None)
    assert chat_client._not_connected().startswith("Not connected.")
