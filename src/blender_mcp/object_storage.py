"""S3-compatible object storage (Garage) for payloads too big for MCP.

MCP messages top out around 25 MB, and even well under that a large payload
burns the calling model's context. So bytes never travel through MCP here:
the server hands out short-lived presigned URLs and the caller (or the
Blender addon) talks to the S3 server directly.

Two clients, one set of credentials (the ``minio`` package is a generic S3
client and works against Garage):

- the *signer* is built for the public endpoint (``S3_PUBLIC_URL``, e.g.
  https://files.blender.bet). A SigV4 signature covers the Host header, so
  URLs must be signed for the host the client will actually connect to;
  Caddy's reverse_proxy passes that Host through unchanged. With a fixed
  region the client signs locally, with no network round trip.
- the *admin* client talks to the internal endpoint (``S3_ENDPOINT``, e.g.
  http://blender-mcp-garage:3900) for bucket setup, listing, stat and delete.
  It's synchronous, so every call goes through ``asyncio.to_thread``.

Garage starts empty (no layout, keys or buckets). When ``GARAGE_ADMIN_URL``
and ``GARAGE_ADMIN_TOKEN`` are set, the first use bootstraps it through the
admin API (see garage_admin.py), idempotently.

Object keys are ``<bus_id>/<32 hex>/<filename>``; every tool checks the key
starts with a bus the caller is a member of.

Expiry, two layers: an S3 lifecycle rule (Garage applies it in a once-a-day
pass) and a pruning task in this process that deletes objects older than
``STORAGE_RETENTION_DAYS`` every ``STORAGE_PRUNE_INTERVAL_S``. The pruner is
what the tests verify, since a lifecycle pass can't be triggered on demand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

DEFAULT_BUCKET = "blender-mcp"
DEFAULT_REGION = "garage"  # Garage's s3_region (packaging/garage.toml)
DEFAULT_RETENTION_DAYS = 7
DEFAULT_URL_EXPIRY_S = 15 * 60
# Output uploads are signed before the render starts, and a render can run
# long (the job path has no hard ceiling), so their PUT URLs live longer.
DEFAULT_OUTPUT_URL_EXPIRY_S = 6 * 3600
# S3 caps a single PUT at 5 GiB.
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024**3
MAX_URL_EXPIRY_S = 7 * 24 * 3600  # SigV4 limit
DEFAULT_PRUNE_INTERVAL_S = 3600

log = logging.getLogger(__name__)

LIFECYCLE_RULE_ID = "blender-mcp-retention"

_NAME_OK = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME_LEN = 128

NOT_CONFIGURED_DETAIL = (
    "Object storage is not configured on this server (S3_ENDPOINT, S3_PUBLIC_URL, "
    "S3_ACCESS_KEY and S3_SECRET_KEY must be set). Fallback: blender_upload sends files up to 50 MB "
    "through the bus in chunks, and outputs stay on the Blender host at the path "
    "each tool returns."
)


class StorageError(Exception):
    """A storage call failed; ``code`` goes into the tool's error JSON."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _int_env(env, name: str, default: int, lo: int = 1, hi: int | None = None) -> int:
    try:
        v = int(env.get(name) or default)
    except ValueError:
        v = default
    v = max(lo, v)
    return min(v, hi) if hi is not None else v


def _endpoint(url: str) -> tuple[str, bool]:
    """``https://files.example.com`` -> ("files.example.com", True)."""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if not parts.netloc:
        raise ValueError(f"not a usable endpoint URL: {url!r}")
    if parts.path not in ("", "/"):
        raise ValueError(f"endpoint must not have a path (got {url!r}); serve S3 at a host root")
    return parts.netloc, parts.scheme == "https"


@dataclass(frozen=True)
class StorageConfig:
    internal_endpoint: str
    internal_secure: bool
    public_endpoint: str
    public_secure: bool
    access_key: str
    secret_key: str
    bucket: str = DEFAULT_BUCKET
    region: str = DEFAULT_REGION
    retention_days: int = DEFAULT_RETENTION_DAYS
    url_expiry_s: int = DEFAULT_URL_EXPIRY_S
    output_url_expiry_s: int = DEFAULT_OUTPUT_URL_EXPIRY_S
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    prune_interval_s: int = DEFAULT_PRUNE_INTERVAL_S
    garage_admin_url: str | None = None
    garage_admin_token: str | None = None
    garage_capacity_gb: int = 100


def load_config(env=None) -> StorageConfig | None:
    """Storage settings from the environment, or None when not configured."""
    env = os.environ if env is None else env
    internal = (env.get("S3_ENDPOINT") or "").strip()
    public = (env.get("S3_PUBLIC_URL") or "").strip()
    access = (env.get("S3_ACCESS_KEY") or "").strip()
    secret = (env.get("S3_SECRET_KEY") or "").strip()
    if not (internal and public and access and secret):
        return None
    i_host, i_secure = _endpoint(internal)
    p_host, p_secure = _endpoint(public)
    return StorageConfig(
        internal_endpoint=i_host, internal_secure=i_secure,
        public_endpoint=p_host, public_secure=p_secure,
        access_key=access, secret_key=secret,
        bucket=(env.get("S3_BUCKET") or DEFAULT_BUCKET).strip(),
        region=(env.get("S3_REGION") or DEFAULT_REGION).strip(),
        retention_days=_int_env(env, "STORAGE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        url_expiry_s=_int_env(env, "STORAGE_URL_EXPIRY_S", DEFAULT_URL_EXPIRY_S,
                              lo=60, hi=MAX_URL_EXPIRY_S),
        output_url_expiry_s=_int_env(env, "STORAGE_OUTPUT_URL_EXPIRY_S",
                                     DEFAULT_OUTPUT_URL_EXPIRY_S, lo=60, hi=MAX_URL_EXPIRY_S),
        max_upload_bytes=_int_env(env, "STORAGE_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES),
        prune_interval_s=_int_env(env, "STORAGE_PRUNE_INTERVAL_S", DEFAULT_PRUNE_INTERVAL_S,
                                  lo=0),
        garage_admin_url=(env.get("GARAGE_ADMIN_URL") or "").strip() or None,
        garage_admin_token=(env.get("GARAGE_ADMIN_TOKEN") or "").strip() or None,
        garage_capacity_gb=_int_env(env, "GARAGE_CAPACITY_GB", 100),
    )


# ---- keys ---------------------------------------------------------------

def safe_name(name) -> str:
    """Same rule as addon/upload_store.sanitize_name: basename, restricted
    characters, no leading dots."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("filename must be a non-empty string")
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_OK.sub("_", base).lstrip(".")[:MAX_NAME_LEN]
    if not base:
        raise ValueError(f"filename {name!r} has no usable characters")
    return base


def make_key(bus_id: str, filename: str) -> str:
    """A fresh key in the bus's namespace: ``<bus_id>/<hex>/<filename>``."""
    if not bus_id or "/" in str(bus_id):
        raise ValueError("bus_id is required and must not contain '/'")
    return f"{bus_id}/{uuid.uuid4().hex}/{safe_name(filename)}"


_KEY_RE = re.compile(r"^(?P<bus>[^/]+)/(?P<id>[0-9a-f]{32})/(?P<name>[A-Za-z0-9_-][A-Za-z0-9._-]*)$")


def parse_key(key) -> tuple[str, str, str]:
    """(bus_id, id, filename), or ValueError for anything not made by make_key."""
    m = _KEY_RE.match(key) if isinstance(key, str) else None
    if not m or len(m["name"]) > MAX_NAME_LEN:
        raise ValueError("object_key must look like <bus_id>/<32 hex>/<filename>")
    return m["bus"], m["id"], m["name"]


def check_key(bus_id: str, key: str) -> str:
    """The key, if it belongs to ``bus_id``. ValueError for a malformed key,
    PermissionError for another bus's key."""
    owner, _, _ = parse_key(key)
    if owner != str(bus_id):
        raise PermissionError("object_key belongs to a different bus")
    return key


# ---- client -------------------------------------------------------------

class ObjectStore:
    """Presigning (public endpoint) plus admin calls (internal endpoint)."""

    def __init__(self, config: StorageConfig):
        from minio import Minio

        self.config = config
        self._signer = Minio(config.public_endpoint, access_key=config.access_key,
                             secret_key=config.secret_key, secure=config.public_secure,
                             region=config.region)
        self._admin = Minio(config.internal_endpoint, access_key=config.access_key,
                            secret_key=config.secret_key, secure=config.internal_secure,
                            region=config.region)
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._pruner: asyncio.Task | None = None

    # Presigning is pure computation (fixed region, local clock).
    def presign_put(self, key: str, expires_s: int | None = None) -> str:
        s = expires_s or self.config.url_expiry_s
        return self._signer.presigned_put_object(self.config.bucket, key,
                                                 expires=timedelta(seconds=s))

    def presign_get(self, key: str, expires_s: int | None = None) -> str:
        s = expires_s or self.config.url_expiry_s
        return self._signer.presigned_get_object(self.config.bucket, key,
                                                 expires=timedelta(seconds=s))

    async def _admin_call(self, fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except StorageError:
            raise
        except Exception as e:
            code = getattr(e, "code", None)
            if code in ("NoSuchKey", "NoSuchObject"):
                raise StorageError("object_not_found", "no such object (it may have expired)") from e
            raise StorageError("storage_unavailable", f"{type(e).__name__}: {e}") from e

    def _ensure_bucket_sync(self) -> None:
        from minio.commonconfig import ENABLED, Filter
        from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

        c = self.config
        if c.garage_admin_url and c.garage_admin_token:
            from .garage_admin import bootstrap

            bootstrap(c.garage_admin_url, c.garage_admin_token, access_key=c.access_key,
                      secret_key=c.secret_key, bucket=c.bucket,
                      capacity_bytes=c.garage_capacity_gb * 1024**3)
        elif not self._admin.bucket_exists(c.bucket):
            self._admin.make_bucket(c.bucket)
        want = Rule(ENABLED, rule_filter=Filter(prefix=""), rule_id=LIFECYCLE_RULE_ID,
                    expiration=Expiration(days=c.retention_days))
        current = self._admin.get_bucket_lifecycle(c.bucket)
        others = [r for r in (current.rules if current else []) if r.rule_id != LIFECYCLE_RULE_ID]
        mine = [r for r in (current.rules if current else []) if r.rule_id == LIFECYCLE_RULE_ID]
        if mine and mine[0].expiration and mine[0].expiration.days == c.retention_days:
            return
        self._admin.set_bucket_lifecycle(c.bucket, LifecycleConfig(others + [want]))

    async def ensure_ready(self) -> None:
        """Bootstrap Garage, the bucket and the retention rule once per process
        (idempotent), and start the pruner."""
        if self._ready:
            return
        async with self._ready_lock:
            if not self._ready:
                await self._admin_call(self._ensure_bucket_sync)
                self._ready = True
                if self.config.prune_interval_s and self._pruner is None:
                    self._pruner = asyncio.create_task(self._prune_loop(),
                                                       name="object-storage-pruner")

    async def _prune_loop(self) -> None:
        while True:
            try:
                n = await self.prune()
                if n:
                    log.info("object storage: pruned %d expired objects", n)
            except Exception as e:  # noqa: BLE001 - keep pruning on the next tick
                log.warning("object storage: prune failed: %s", e)
            await asyncio.sleep(self.config.prune_interval_s)

    async def prune(self, max_age: timedelta | None = None, now: datetime | None = None) -> int:
        """Delete objects older than ``max_age`` (default: the retention period).

        Doesn't rely on the server's lifecycle support: it lists the bucket
        and deletes by each object's upload time.
        """
        age = max_age if max_age is not None else timedelta(days=self.config.retention_days)
        cutoff = (now or datetime.now(UTC)) - age
        bucket = self.config.bucket

        def run():
            n = 0
            for o in self._admin.list_objects(bucket, recursive=True):
                if o.last_modified is not None and o.last_modified < cutoff:
                    self._admin.remove_object(bucket, o.object_name)
                    n += 1
            return n

        return await self._admin_call(run)

    async def stat(self, key: str) -> dict:
        await self.ensure_ready()
        o = await self._admin_call(self._admin.stat_object, self.config.bucket, key)
        return _describe(key, o.size, o.last_modified, o.content_type, self.config.retention_days)

    async def list(self, bus_id: str, limit: int = 1000) -> list[dict]:
        await self.ensure_ready()

        def run():
            out = []
            for o in self._admin.list_objects(self.config.bucket, prefix=f"{bus_id}/",
                                              recursive=True):
                out.append(_describe(o.object_name, o.size, o.last_modified, None,
                                     self.config.retention_days))
                if len(out) >= limit:
                    break
            return out

        return await self._admin_call(run)

    async def get_bytes(self, key: str, max_bytes: int) -> bytes:
        """Read a small object over the internal endpoint (the chat's vision
        step reads screenshots this way). Refuses objects over ``max_bytes``."""
        await self.ensure_ready()

        def run():
            r = self._admin.get_object(self.config.bucket, key)
            try:
                data = r.read(max_bytes + 1)
            finally:
                r.close()
                r.release_conn()
            if len(data) > max_bytes:
                raise StorageError("object_too_large", f"object is over {max_bytes} bytes")
            return data

        return await self._admin_call(run)

    async def delete(self, key: str) -> None:
        await self.ensure_ready()
        await self.stat(key)  # remove_object is silent on a missing key
        await self._admin_call(self._admin.remove_object, self.config.bucket, key)


def _describe(key, size, last_modified, content_type, retention_days) -> dict:
    d = {"object_key": key, "name": key.rsplit("/", 1)[-1], "size": size}
    if last_modified is not None:
        d["last_modified"] = last_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Deleted by the next prune (or lifecycle pass) after this time.
        d["expires_after"] = (last_modified + timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    if content_type:
        d["content_type"] = content_type
    return d


# ---- tool outputs stored after the fact ---------------------------------
#
# A dispatch tool called with store=True gets a presigned PUT URL in its
# params under ``_store``; the addon executor strips it before the handler
# runs, uploads the file the handler reports, and adds a ``stored`` entry
# to the result. The server then adds a presigned download URL to that entry.

def output_name(path: str | None, default: str) -> str:
    """The object's file name: the basename of a caller-given host path, else a default."""
    if path:
        try:
            return safe_name(path)
        except ValueError:
            pass
    return default


def output_params(store: ObjectStore, bus_id: str, filename: str,
                  content_type: str | None = None) -> dict:
    key = make_key(bus_id, filename)
    d = {"url": store.presign_put(key, store.config.output_url_expiry_s), "object_key": key}
    if content_type:
        d["content_type"] = content_type
    return d


def attach_download(dispatch_json: str, store: ObjectStore | None) -> str:
    """Add a download URL to a completed reply's ``stored`` entry, keeping the
    reply's shape (``result`` stays a JSON string when it was one)."""
    import json

    try:
        reply = json.loads(dispatch_json)
    except ValueError:
        return dispatch_json
    if not isinstance(reply, dict) or reply.get("status") != "completed":
        return dispatch_json
    raw = reply.get("result")
    inner = raw
    if isinstance(raw, str):
        try:
            inner = json.loads(raw)
        except ValueError:
            return dispatch_json
    if not isinstance(inner, dict):
        return dispatch_json
    stored = inner.get("stored")
    if store is None:
        inner["stored"] = {"state": "not_configured", "detail": NOT_CONFIGURED_DETAIL}
    elif isinstance(stored, dict) and stored.get("object_key") and \
            stored.get("state") in ("uploaded", "uploading"):
        stored["download_url"] = store.presign_get(stored["object_key"])
        stored["download_expires_in"] = store.config.url_expiry_s
        if stored["state"] == "uploading":
            stored["note"] = ("upload still running in Blender; check blender_object_transfer_status "
                              "with transfer_id, or re-sign later with blender_create_download_url")
    elif not isinstance(stored, dict):
        inner["stored"] = {"state": "failed",
                           "error": "the addon did not store the output (update the addon)"}
    reply["result"] = json.dumps(inner) if isinstance(raw, str) else inner
    return json.dumps(reply)


@lru_cache(maxsize=1)
def get_store() -> ObjectStore | None:
    """The process's store, or None when storage isn't configured."""
    config = load_config()
    return ObjectStore(config) if config else None
