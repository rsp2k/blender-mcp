"""Server-side Poly Haven API reads (no Blender needed).

Poly Haven is a free CC0 API with no key. Read-only lookups (search,
categories, asset info with real-world dimensions) run here so they work
when the addon's "Use assets from Poly Haven" switch is off or no Blender
is connected; only downloads, which import into the scene, need the addon.
"""

from __future__ import annotations

import json

import httpx

API = "https://api.polyhaven.com"
# Poly Haven asks API clients to identify themselves.
HEADERS = {"User-Agent": "blender-mcp"}
ASSET_TYPES = ("hdris", "textures", "models", "all")
SEARCH_LIMIT = 20


def dimensions_m(info) -> list[float] | None:
    """[w, h] in metres from an /info or /assets entry's ``dimensions`` (mm).

    Mirrors addon/polyhaven_helpers.py; the installed server can't import
    the addon tree.
    """
    if not isinstance(info, dict):
        return None
    dims = info.get("dimensions")
    if not isinstance(dims, (list, tuple)) or len(dims) < 2:
        return None
    try:
        w, h = float(dims[0]), float(dims[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return [round(w / 1000.0, 4), round(h / 1000.0, 4)]


def addon_unavailable(result_json: str) -> bool:
    """True when a dispatch reply means "the addon can't answer this".

    Covers the Poly Haven switch being off (current and pre-hint addons)
    and no Blender being connected, which are the cases a server-side
    lookup can stand in for.
    """
    try:
        data = json.loads(result_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("status") in ("no_client", "no_live_client"):
        return True
    text = " ".join(str(data.get(k) or "") for k in ("error", "message"))
    return "is disabled: Poly Haven" in text or "Unknown command type" in text


def summarize_assets(assets: dict, limit: int = SEARCH_LIMIT) -> dict:
    """Same shape as the addon's search reply, with dimensions_m added."""
    limited = {}
    for key, value in list(assets.items())[:limit]:
        if isinstance(value, dict):
            value = dict(value, dimensions_m=dimensions_m(value))
        limited[key] = value
    return {"assets": limited, "total_count": len(assets), "returned_count": len(limited)}


def summarize_info(asset_id: str, info: dict) -> dict:
    """The fields that matter for placing an asset, plus metres."""
    type_names = {0: "hdris", 1: "textures", 2: "models"}
    return {
        "id": asset_id,
        "name": info.get("name"),
        "type": type_names.get(info.get("type"), info.get("type")),
        "dimensions_m": dimensions_m(info),
        "dimensions_mm": info.get("dimensions"),
        "categories": info.get("categories"),
        "tags": info.get("tags"),
        "max_resolution": info.get("max_resolution"),
        "evs_cap": info.get("evs_cap"),  # HDRIs: dynamic range in EV
        "authors": info.get("authors"),
    }


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
        r = await client.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def search(asset_type: str | None = None, categories: str | None = None) -> dict:
    params = {}
    if asset_type and asset_type != "all":
        if asset_type not in ASSET_TYPES:
            raise ValueError(f"asset_type must be one of {', '.join(ASSET_TYPES)}")
        params["type"] = asset_type
    if categories:
        params["categories"] = categories
    return summarize_assets(await _get("/assets", params))


async def categories(asset_type: str) -> dict:
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"asset_type must be one of {', '.join(ASSET_TYPES)}")
    return {"categories": await _get(f"/categories/{asset_type}")}


async def info(asset_id: str) -> dict:
    return summarize_info(asset_id, await _get(f"/info/{asset_id}"))


async def server_side(kind: str, **kwargs) -> str:
    """Run a lookup here and wrap it like a dispatch reply."""
    fn = {"search": search, "categories": categories, "info": info}[kind]
    try:
        result = await fn(**kwargs)
    except (httpx.HTTPError, ValueError) as e:
        return json.dumps({"status": "error", "source": "server", "error": str(e)})
    return json.dumps({"status": "success", "source": "server", "result": result})
