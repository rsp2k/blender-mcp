"""addon/object_store.py against a tiny local HTTP server (no docker, no bpy)."""

import http.server
import threading
from typing import ClassVar

import pytest

from addon import object_store as ob


class Handler(http.server.BaseHTTPRequestHandler):
    store: ClassVar[dict] = {}
    seen: ClassVar[list] = []

    def do_PUT(self):
        self.seen.append(dict(self.headers))
        n = int(self.headers.get("Content-Length", 0))
        self.store[self.path] = self.rfile.read(n)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        data = self.store.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<Error>NoSuchKey</Error>")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Handler.store, Handler.seen = {}, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_put_sends_content_length_not_chunked(server, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x" * 3_000_000)
    out = ob.put_file(f"{server}/b/k", f, "image/png")
    h = Handler.seen[0]
    assert h["Content-Length"] == "3000000" and "Transfer-Encoding" not in h
    assert h["Content-Type"] == "image/png"
    assert out["size"] == 3_000_000 and Handler.store["/b/k"] == f.read_bytes()


def test_empty_file_uploads(server, tmp_path):
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    assert ob.put_file(f"{server}/e", f)["size"] == 0 and Handler.store["/e"] == b""


def test_download_size_mismatch_leaves_nothing(server, tmp_path):
    Handler.store["/k"] = b"abc"
    with pytest.raises(RuntimeError, match="size mismatch"):
        ob.get_to_dir(f"{server}/k", tmp_path, "k.bin", expected_size=4)
    assert list(tmp_path.iterdir()) == []


def test_download_respects_overwrite(server, tmp_path):
    Handler.store["/k"] = b"new"
    (tmp_path / "k.bin").write_bytes(b"old")
    with pytest.raises(ValueError, match="already exists"):
        ob.get_to_dir(f"{server}/k", tmp_path, "k.bin", overwrite=False)
    assert ob.get_to_dir(f"{server}/k", tmp_path, "k.bin")["size"] == 3
    assert (tmp_path / "k.bin").read_bytes() == b"new"


def test_attach_stored_never_raises(server, tmp_path):
    store = {"url": f"{server}/x", "object_key": "b/" + "0" * 32 + "/r.png"}
    out = ob.attach_stored({"status": "ok"}, store)
    assert out["stored"]["state"] == "failed" and "no output file" in out["stored"]["error"]
    out = ob.attach_stored({"filepath": str(tmp_path / "missing.png")}, store)
    assert out["stored"]["state"] == "failed"
    out = ob.attach_stored({"filepath": "/x.png"}, "garbage")
    assert out["stored"]["state"] == "failed"


def test_attach_stored_finds_each_output_key(server, tmp_path):
    f = tmp_path / "m.glb"
    f.write_bytes(b"glTF")
    for k in ob.OUTPUT_KEYS:
        out = ob.attach_stored({k: str(f)}, {"url": f"{server}/{k}", "object_key": "key"})
        assert out["stored"]["state"] == "uploaded", out
    assert Handler.seen[-1]["Content-Type"] == "model/gltf-binary"


def test_background_failure_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "SYNC_LIMIT", 0)

    def boom(progress):
        raise RuntimeError("network down")

    t = ob.run_transfer("download", "x", 10, boom)
    for _ in range(200):
        rec = ob.TRANSFERS.get(t["transfer_id"])
        if rec["state"] != "running":
            break
        threading.Event().wait(0.01)
    assert rec["state"] == "failed" and rec["error"] == "network down"


def test_transfer_records_are_bounded(monkeypatch):
    tr = ob.Transfers()
    monkeypatch.setattr(ob, "MAX_TRACKED", 5)
    ids = [tr.start("upload", str(i), 1) for i in range(5)]
    for i in ids:
        tr.update(i, state="done")
    tr.start("upload", "new", 1)
    assert tr.get(ids[0]) is None and len(tr._items) == 5
