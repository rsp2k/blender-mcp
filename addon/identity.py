"""Persistent client identity for the message bus.

The bus uses a sticky UUID so the same Blender install is recognizable
across restarts. The UUID file lives in Blender's user config dir so it
travels with the install but isn't tracked by source control.

This module imports `bpy` lazily inside the constructor — that lets the
module be importable in tests and CI without a Blender runtime, and only
hits `bpy.utils.resource_path` when an instance is actually created.
"""

from __future__ import annotations

import os
import uuid
from typing import Optional


class StickyUUIDManager:
    """Manages persistent client UUID for message bus registration.

    The UUID is written to `<USER>/config/blender_mcp_uuid.txt`. On load,
    if the file content is a valid-length UUID string, it's reused; the
    bus-side client ID is always prefixed with ``blender-`` so it's
    distinguishable from other client types on the bus.
    """

    UUID_PREFIX = "blender-"
    UUID_FILENAME = "blender_mcp_uuid.txt"

    def __init__(self, uuid_file: Optional[str] = None) -> None:
        if uuid_file is None:
            import bpy  # lazy: only when actually used inside Blender
            uuid_file = os.path.join(
                bpy.utils.resource_path("USER"), "config", self.UUID_FILENAME
            )
        self.uuid_file = uuid_file
        self.client_id = self._load_or_generate_uuid()

    def _load_or_generate_uuid(self) -> str:
        """Load existing UUID or generate a new one."""
        try:
            if os.path.exists(self.uuid_file):
                with open(self.uuid_file, "r") as f:
                    stored_uuid = f.read().strip()
                if len(stored_uuid) == 36:  # canonical UUID length
                    print(f"Loaded existing client UUID: {self.UUID_PREFIX}{stored_uuid[:8]}")
                    return f"{self.UUID_PREFIX}{stored_uuid}"
        except Exception as e:
            print(f"Error loading UUID: {e}")

        new_uuid = str(uuid.uuid4())
        try:
            os.makedirs(os.path.dirname(self.uuid_file), exist_ok=True)
            with open(self.uuid_file, "w") as f:
                f.write(new_uuid)
            print(f"Generated new client UUID: {self.UUID_PREFIX}{new_uuid[:8]}")
        except Exception as e:
            print(f"Error saving UUID: {e}")

        return f"{self.UUID_PREFIX}{new_uuid}"

    def get_client_id(self) -> str:
        return self.client_id
