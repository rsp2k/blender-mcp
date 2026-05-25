"""Blender UI subpackage — panel + operators.

Two modules:

  panel.py     — BLENDERMCP_PT_Panel (the View3D sidebar panel)
  operators.py — StartServer, StopServer, SetFreeTrialHyper3DAPIKey

The top-level `register()` in addon/__init__.py (or addon.py during the
transitional phases) imports `CLASSES` from here and registers each
with bpy.utils.register_class.
"""

from __future__ import annotations

from .operators import (
    BLENDERMCP_OT_Login,
    BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
    BLENDERMCP_OT_TestConnection,
)
from .panel import BLENDERMCP_PT_Panel

#: All bpy.types classes the addon registers; iterated by register() / unregister().
CLASSES = (
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_TestConnection,
    BLENDERMCP_OT_Login,
    BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)

__all__ = [
    "CLASSES",
    "BLENDERMCP_PT_Panel",
    "BLENDERMCP_OT_Login",
    "BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey",
    "BLENDERMCP_OT_StartServer",
    "BLENDERMCP_OT_StopServer",
    "BLENDERMCP_OT_TestConnection",
]
