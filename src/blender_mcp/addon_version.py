"""Latest published addon version, exposed to the register_client response.

Read from ``addon/_version.py`` at import time so tools can hand each
connecting addon a hint about whether it's behind the current release.
Regex-based read rather than ``import addon._version`` so we never risk
pulling ``bpy`` (or anything else the addon package brings along) into
the server process.

If the addon source isn't on disk beside the server (some container
builds strip it), the ``LATEST_ADDON_VERSION`` env var wins, and if
that's also missing we fall back to ``None``. The register_client
handler omits the hint when it's ``None`` so old-shape clients still
see a clean response.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_VERSION_RE = re.compile(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"')


def _read_from_disk() -> Optional[str]:
    # src/blender_mcp/addon_version.py -> parents[2] is the repo root
    # in the source layout; container builds that keep the addon package
    # alongside the wheel land in the same shape.
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "addon" / "_version.py",  # repo layout
        here.parents[3] / "addon" / "_version.py",  # in-container /app/addon
    ]
    for path in candidates:
        try:
            m = _VERSION_RE.search(path.read_text())
        except (FileNotFoundError, OSError):
            continue
        if m:
            return m.group(1)
    return None


LATEST_ADDON_VERSION: Optional[str] = (
    os.environ.get("LATEST_ADDON_VERSION") or _read_from_disk()
)
"""Semver string of the latest addon release the server knows about.

``None`` when the addon source can't be located and no env override is
set. Callers should treat ``None`` as "don't send the hint at all"
rather than as "no update needed."
"""


ADDON_DOWNLOAD_URL: str = os.environ.get(
    "ADDON_DOWNLOAD_URL",
    "https://docs.blender.bet/how-to/install-addon/",
)
"""Where to point users when they're behind. The docs page explains both
install paths (extension repo + legacy) so the banner doesn't have to
guess which shape the user is on. Overridable via env var."""
