"""Object storage: config, key namespacing, presigning, bus scoping, and the
not-configured path. No MinIO needed; see test_object_storage_minio.py for
the live round trip."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from blender_mcp import dispatch_component as dc
from blender_mcp import object_storage as os_
from blender_mcp import object_tools as ot

ENV = {
    "MINIO_ENDPOINT": "http://blender-mcp-minio:9000",
    "MINIO_PUBLIC_URL": "https://files.example.com",
    "MINIO_ROOT_USER": "root",
    "MINIO_ROOT_PASSWORD": "secret-secret",
}


def store(**overrides):
    return os_.ObjectStore(os_.load_config({**ENV, **overrides}))


# ---- config ---------------------------------------------------------------

def test_not_configured_without_endpoint_or_credentials():
    assert os_.load_config({}) is None
    for missing in ENV:
        assert os_.load_config({k: v for k, v in ENV.items() if k != missing}) is None


def test_config_parses_endpoints_and_defaults():
    c = os_.load_config(ENV)
    assert (c.internal_endpoint, c.internal_secure) == ("blender-mcp-minio:9000", False)
    assert (c.public_endpoint, c.public_secure) == ("files.example.com", True)
    assert c.access_key == "root" and c.bucket == "blender-mcp"
    assert c.retention_days == 7 and c.url_expiry_s == 900


def test_explicit_keys_override_root_and_expiry_is_clamped():
    c = os_.load_config({**ENV, "MINIO_ACCESS_KEY": "svc", "MINIO_SECRET_KEY": "svc-secret",
                         "STORAGE_URL_EXPIRY_S": "99999999", "STORAGE_RETENTION_DAYS": "junk"})
    assert c.access_key == "svc" and c.secret_key == "svc-secret"
    assert c.url_expiry_s == os_.MAX_URL_EXPIRY_S and c.retention_days == 7


def test_public_url_with_a_path_is_rejected():
    with pytest.raises(ValueError, match="path"):
        os_.load_config({**ENV, "MINIO_PUBLIC_URL": "https://example.com/minio"})


# ---- keys -----------------------------------------------------------------

def test_keys_are_namespaced_by_bus_and_unique():
    a, b = os_.make_key("bus-1", "model.glb"), os_.make_key("bus-1", "model.glb")
    assert a != b
    bus, oid, name = os_.parse_key(a)
    assert bus == "bus-1" and len(oid) == 32 and name == "model.glb"


@pytest.mark.parametrize("raw,clean", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\me\\plan view.png", "plan_view.png"),
    (".hidden", "hidden"),
])
def test_filenames_are_sanitised(raw, clean):
    assert os_.parse_key(os_.make_key("b", raw))[2] == clean


def test_check_key_enforces_the_bus():
    key = os_.make_key("bus-a", "x.png")
    assert os_.check_key("bus-a", key) == key
    with pytest.raises(PermissionError):
        os_.check_key("bus-b", key)
    for bad in ("bus-a/x.png", "bus-a/../bus-b/" + "0" * 32 + "/x", "", None,
                "bus-a/" + "0" * 32 + "/.x", "bus-a/" + "g" * 32 + "/x"):
        with pytest.raises(ValueError):
            os_.check_key("bus-a", bad)


def test_output_name():
    assert os_.output_name("/tmp/x/My Render.png", "render.png") == "My_Render.png"
    assert os_.output_name(None, "render.png") == "render.png"
    assert os_.output_name("/tmp/x/", "render.png") == "render.png"


# ---- presigning -----------------------------------------------------------

def test_presigned_urls_target_the_public_host_without_network():
    # The internal endpoint doesn't resolve here; presigning must not touch it.
    s = store(MINIO_ENDPOINT="http://nowhere.invalid:9000")
    key = os_.make_key("bus-1", "a b.png")
    put = urlsplit(s.presign_put(key))
    assert put.scheme == "https" and put.netloc == "files.example.com"
    assert put.path == f"/blender-mcp/{key}".replace(" ", "%20")
    q = parse_qs(put.query)
    assert q["X-Amz-Expires"] == ["900"]
    assert q["X-Amz-SignedHeaders"] == ["host"]
    assert "/us-east-1/s3/aws4_request" in q["X-Amz-Credential"][0]
    get = parse_qs(urlsplit(s.presign_get(key, 60)).query)
    assert get["X-Amz-Expires"] == ["60"]


# ---- output post-processing -----------------------------------------------

def reply(inner, as_string=True):
    return json.dumps({"status": "completed", "job_id": "j",
                       "result": json.dumps(inner) if as_string else inner})


def test_attach_download_adds_url_and_keeps_shape():
    s = store()
    key = os_.make_key("b", "render.png")
    out = json.loads(os_.attach_download(
        reply({"filepath": "/tmp/r.png", "stored": {"object_key": key, "state": "uploaded"}}), s))
    assert isinstance(out["result"], str)
    stored = json.loads(out["result"])["stored"]
    assert urlsplit(stored["download_url"]).netloc == "files.example.com"
    assert stored["download_expires_in"] == 900


def test_attach_download_reports_missing_storage_and_old_addons():
    out = json.loads(os_.attach_download(reply({"filepath": "/r.png"}, as_string=False), None))
    assert out["result"]["stored"]["state"] == "not_configured"
    out = json.loads(os_.attach_download(reply({"filepath": "/r.png"}), store()))
    assert json.loads(out["result"])["stored"]["state"] == "failed"


def test_attach_download_leaves_failures_and_timeouts_alone():
    for r in ('{"status": "timeout", "job_id": "j"}', "not json",
              json.dumps({"status": "error", "error": "x"})):
        assert os_.attach_download(r, store()) == r
    failed = reply({"stored": {"object_key": "k", "state": "failed", "error": "boom"}})
    assert "download_url" not in os_.attach_download(failed, store())


# ---- _call with store_as ---------------------------------------------------

@pytest.fixture
def dispatch_env(monkeypatch):
    sent = {}

    async def fake_resolve_bus(user_id, bus_id):
        return {"ok": True, "bus": object(), "bus_id": "bus-1"}

    async def fake_dispatch(bus, bus_id, command, params, target, timeout, caller_sub=None):
        sent["params"] = params
        key = params.get("_store", {}).get("object_key")
        return reply({"filepath": "/tmp/r.png",
                      **({"stored": {"object_key": key, "state": "uploaded"}} if key else {})})

    monkeypatch.setattr(dc, "check_role_or_reject", lambda *a: None)
    monkeypatch.setattr(dc, "_resolve_user_id", lambda ctx: "user-1")
    monkeypatch.setattr(dc, "resolve_bus", fake_resolve_bus)
    monkeypatch.setattr(dc, "_dispatch", fake_dispatch)
    return sent


async def test_call_signs_an_output_upload_in_the_callers_bus(dispatch_env, monkeypatch):
    s = store()

    async def ready():
        return None

    monkeypatch.setattr(s, "ensure_ready", ready)
    monkeypatch.setattr(dc, "get_store", lambda: s)
    out = json.loads(await dc.BlenderDispatchComponent()._call(
        None, "render", {"engine": "EEVEE"}, None, 5, store_as="render.png"))
    st = dispatch_env["params"]["_store"]
    assert st["object_key"].startswith("bus-1/") and st["object_key"].endswith("/render.png")
    assert parse_qs(urlsplit(st["url"]).query)["X-Amz-Expires"] == ["21600"]
    assert "download_url" in json.loads(out["result"])["stored"]


async def test_call_without_storage_still_runs_the_command(dispatch_env, monkeypatch):
    monkeypatch.setattr(dc, "get_store", lambda: None)
    out = json.loads(await dc.BlenderDispatchComponent()._call(
        None, "render", {}, None, 5, store_as="render.png"))
    assert "_store" not in dispatch_env["params"]
    assert json.loads(out["result"])["stored"]["state"] == "not_configured"


async def test_call_without_store_as_is_unchanged(dispatch_env, monkeypatch):
    monkeypatch.setattr(dc, "get_store", lambda: pytest.fail("store looked up"))
    out = json.loads(await dc.BlenderDispatchComponent()._call(None, "render", {}, None, 5))
    assert "stored" not in json.loads(out["result"])


# ---- tools ----------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.config = os_.load_config(ENV)
        self.real = os_.ObjectStore(self.config)
        self.deleted = []

    async def ensure_ready(self):
        return None

    def presign_put(self, key, expires_s=None):
        return self.real.presign_put(key, expires_s)

    def presign_get(self, key, expires_s=None):
        return self.real.presign_get(key, expires_s)

    async def stat(self, key):
        return {"object_key": key, "name": key.rsplit("/", 1)[-1], "size": 1234}

    async def list(self, bus_id):
        return [{"object_key": f"{bus_id}/{'a' * 32}/x.png", "size": 3,
                 "last_modified": "2026-09-27T00:00:00Z"}]

    async def delete(self, key):
        self.deleted.append(key)


@pytest.fixture
def tools(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(ot, "get_store", lambda: fake)

    async def fake_resolve(ctx, tool, bus_id):
        if bus_id not in (None, "bus-a"):
            return None, json.dumps({"ok": False, "error": "not_a_member"}), None
        return object(), "bus-a", "user-1"

    monkeypatch.setattr(ot, "_resolve", fake_resolve)
    c = ot.BlenderObjectStorageComponent()
    c.store = fake
    return c


async def test_tools_report_missing_storage_with_the_fallback(monkeypatch):
    monkeypatch.setattr(ot, "get_store", lambda: None)
    c = ot.BlenderObjectStorageComponent()
    for call in (c.create_upload_url("a.glb"), c.create_download_url("k"), c.list_objects(),
                 c.delete_object("k"), c.fetch_object_to_blender("k")):
        out = json.loads(await call)
        assert out["error"] == "storage_not_configured"
        assert "blender_upload" in out["detail"]


async def test_upload_url_is_in_the_callers_bus(tools):
    out = json.loads(await tools.create_upload_url("plan.pdf", content_type="application/pdf",
                                                   size=10))
    assert out["status"] == "ok" and out["object_key"].startswith("bus-a/")
    assert out["headers"] == {"Content-Type": "application/pdf"} and out["method"] == "PUT"
    assert urlsplit(out["upload_url"]).netloc == "files.example.com"


async def test_upload_url_checks_size(tools):
    out = json.loads(await tools.create_upload_url("big.bin", size=6 * 1024**3))
    assert out["error"] == "too_large"
    assert json.loads(await tools.create_upload_url("x", size=-1))["error"] == "invalid_argument"


async def test_other_buses_keys_are_refused(tools):
    foreign = os_.make_key("bus-b", "secret.png")
    for call in (tools.create_download_url(foreign), tools.delete_object(foreign),
                 tools.fetch_object_to_blender(foreign)):
        assert json.loads(await call)["error"] == "forbidden"
    assert not tools.store.deleted
    # Asking for a bus you're not in fails at resolve_bus, before any key check.
    out = json.loads(await tools.create_download_url(foreign, bus_id="bus-b"))
    assert out["error"] == "not_a_member"


async def test_download_list_delete_in_own_bus(tools):
    key = os_.make_key("bus-a", "r.png")
    out = json.loads(await tools.create_download_url(key))
    assert out["size"] == 1234 and "download_url" in out
    listed = json.loads(await tools.list_objects())
    assert listed["bus_id"] == "bus-a" and listed["total_bytes"] == 3
    assert json.loads(await tools.delete_object(key))["deleted"] == key
    assert tools.store.deleted == [key]


async def test_fetch_dispatches_a_presigned_get_and_waits(tools, monkeypatch):
    key = os_.make_key("bus-a", "model.glb")
    calls = []

    async def fake_dispatch(bus, bus_id, command, params, target, timeout, caller_sub=None):
        calls.append((command, params))
        if command == "fetch_object":
            return json.dumps({"status": "completed", "target_uuid": "t1", "result": json.dumps(
                {"state": "running", "transfer_id": "abc", "name": "model.glb"})})
        done = {"state": "done", "transfer_id": "abc",
                "result": {"name": "model.glb", "path": "/up/model.glb", "size": 1234}}
        return json.dumps({"status": "completed", "result": json.dumps(done)})

    monkeypatch.setattr(ot, "_dispatch", fake_dispatch)
    monkeypatch.setattr(ot, "TRANSFER_POLL_S", 0)
    out = json.loads(await tools.fetch_object_to_blender(key))
    assert out["status"] == "ok" and out["path"] == "/up/model.glb" and out["target_uuid"] == "t1"
    cmd, params = calls[0]
    assert cmd == "fetch_object" and params["size"] == 1234 and params["bus_dir"] == "bus-a"
    assert urlsplit(params["url"]).netloc == "files.example.com"
    assert calls[1] == ("transfer_status", {"transfer_id": "abc"})


def test_tools_register_with_the_blender_prefix():
    from fastmcp import FastMCP

    server = FastMCP("t")
    ot.BlenderObjectStorageComponent().register_tools(mcp_server=server, prefix="blender")
    import asyncio

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"blender_create_upload_url", "blender_create_download_url", "blender_list_objects",
            "blender_delete_object", "blender_fetch_object_to_blender",
            "blender_object_transfer_status"} <= names

