"""Shared addon constants.

Kept separate so handler modules (PolyHaven, Hyper3D, etc.) can import
just the constant they need without pulling in `bpy` or the bus client.
"""

from __future__ import annotations

import requests

# Hyper3D Rodin: the public "free trial" API key bundled with the addon.
# Used as the default value of the AddonPreferences `hyper3d_api_key` field,
# and to distinguish 'private' vs 'free_trial' usage in panel labels.
RODIN_FREE_TRIAL_KEY = "k9TcfFoEhNd9cCPP2guHAHHHkctZHIRhZDywZ1euGUXwihbYLpOjQhofby80NJez"

# Poly Haven requires a custom User-Agent. Reuse for any handler that
# hits its public API.
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})
