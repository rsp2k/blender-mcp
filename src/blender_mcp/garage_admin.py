"""Idempotent single-node Garage bootstrap over its admin API (v2).

A fresh Garage node has no layout, keys or buckets. This does the equivalent
of ``garage layout assign`` + ``layout apply``, ``key import``, ``bucket
create`` and ``bucket allow``, each only when missing, so it is safe to run
on every server start. The access key is imported rather than generated so
it can live in .env (Garage wants ``GK`` + 24 hex for the id, 64 hex for the
secret). Synchronous; ObjectStore calls it through asyncio.to_thread.
"""

from __future__ import annotations

import requests

TIMEOUT_S = 15
ZONE = "dc1"


class GarageAdminError(RuntimeError):
    pass


def _api(base: str, token: str):
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    base = base.rstrip("/")

    def call(method: str, endpoint: str, *, params=None, body=None, ok=(200,)):
        r = s.request(method, f"{base}/v2/{endpoint}", params=params, json=body,
                      timeout=TIMEOUT_S)
        if r.status_code not in ok:
            raise GarageAdminError(f"{endpoint}: HTTP {r.status_code} {r.text[:300]}")
        return r

    return call


def bootstrap(base: str, token: str, *, access_key: str, secret_key: str, bucket: str,
              capacity_bytes: int) -> dict:
    """Make sure this node has a role, the key exists, and the key owns the bucket.
    Returns what was changed (empty lists when everything already existed)."""
    call = _api(base, token)
    changed = []

    layout = call("GET", "GetClusterLayout").json()
    if not layout.get("roles"):
        status = call("GET", "GetClusterStatus").json()
        nodes = status.get("nodes") or []
        if len(nodes) != 1:
            raise GarageAdminError(f"expected a single Garage node, found {len(nodes)}")
        call("POST", "UpdateClusterLayout", body={"roles": [
            {"id": nodes[0]["id"], "zone": ZONE, "capacity": capacity_bytes, "tags": []}]})
        call("POST", "ApplyClusterLayout", body={"version": layout.get("version", 0) + 1})
        changed.append("layout")

    if call("GET", "GetKeyInfo", params={"id": access_key}, ok=(200, 400, 404)).status_code != 200:
        call("POST", "ImportKey", body={"accessKeyId": access_key, "secretAccessKey": secret_key,
                                        "name": "blender-mcp"})
        changed.append("key")

    r = call("GET", "GetBucketInfo", params={"globalAlias": bucket}, ok=(200, 404))
    if r.status_code == 404:
        info = call("POST", "CreateBucket", body={"globalAlias": bucket}).json()
        changed.append("bucket")
    else:
        info = r.json()

    grant = next((k for k in info.get("keys", []) if k.get("accessKeyId") == access_key), None)
    perms = (grant or {}).get("permissions") or {}
    if not (perms.get("read") and perms.get("write") and perms.get("owner")):
        call("POST", "AllowBucketKey", body={
            "bucketId": info["id"], "accessKeyId": access_key,
            "permissions": {"read": True, "write": True, "owner": True}})
        changed.append("grant")
    return {"changed": changed}
