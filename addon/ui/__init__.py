"""Blender UI subpackage — panel + operators.

Two modules:

  panel.py     — BLENDERMCP_PT_Panel (the View3D sidebar panel — minimal:
                 status, Connect/Disconnect, asset toggles only)
  operators.py — OAuthLogin, Logout, StartServer, StopServer, TestConnection,
                 SetFreeTrialHyper3DAPIKey

The top-level `register()` in addon/__init__.py imports `CLASSES` from
here and registers each with bpy.utils.register_class.
"""

from __future__ import annotations

from .operators import (
    BLENDERMCP_OT_CopyClientUUID,
    BLENDERMCP_OT_CreateBus,
    BLENDERMCP_OT_DismissFatalError,
    BLENDERMCP_OT_InviteToBus,
    BLENDERMCP_OT_JoinBus,
    BLENDERMCP_OT_LeaveBus,
    BLENDERMCP_OT_Logout,
    BLENDERMCP_OT_OAuthLogin,
    BLENDERMCP_OT_RefreshBuses,
    BLENDERMCP_OT_ReLogin,
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
    BLENDERMCP_OT_OAuthLogin,
    BLENDERMCP_OT_Logout,
    BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
    BLENDERMCP_OT_RefreshBuses,
    BLENDERMCP_OT_CreateBus,
    BLENDERMCP_OT_JoinBus,
    BLENDERMCP_OT_LeaveBus,
    BLENDERMCP_OT_InviteToBus,
    BLENDERMCP_OT_CopyClientUUID,
    BLENDERMCP_OT_DismissFatalError,
    BLENDERMCP_OT_ReLogin,
)

__all__ = [
    "CLASSES",
    "BLENDERMCP_PT_Panel",
    "BLENDERMCP_OT_OAuthLogin",
    "BLENDERMCP_OT_Logout",
    "BLENDERMCP_OT_SetFreeTrialHyper3DAPIKey",
    "BLENDERMCP_OT_StartServer",
    "BLENDERMCP_OT_StopServer",
    "BLENDERMCP_OT_TestConnection",
    "BLENDERMCP_OT_RefreshBuses",
    "BLENDERMCP_OT_CreateBus",
    "BLENDERMCP_OT_JoinBus",
    "BLENDERMCP_OT_LeaveBus",
    "BLENDERMCP_OT_InviteToBus",
    "BLENDERMCP_OT_CopyClientUUID",
    "BLENDERMCP_OT_DismissFatalError",
    "BLENDERMCP_OT_ReLogin",
]
