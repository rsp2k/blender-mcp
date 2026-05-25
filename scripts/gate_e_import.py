"""Gate E — runtime import check for the addon/ package outside Blender.

We can't import addon.* directly on a machine without Blender because
several modules do `import bpy` at the top level. This script injects a
minimal `bpy`/`bmesh`/`mathutils` stub into sys.modules before any addon
import, which is enough to satisfy import time for the modules that
participate in the bus client flow.

Run from repo root::

    uv run python scripts/gate_e_import.py

Exit 0 on success, non-zero on any error. Designed to be cheap (well
under a second) so it can be wired into a pre-commit or CI gate.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Allow `import addon` regardless of cwd — addon/ sits next to this scripts/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def install_bpy_stubs() -> None:
    """Install minimal sys.modules entries so addon.* import time succeeds.

    Stubs enough for ``addon.preferences`` (Phase 8+) which imports both
    ``bpy.types.AddonPreferences`` and ``bpy.props.{String,Bool,Enum}Property``.
    Those classes/functions are referenced at class-body evaluation time
    when defining ``BlenderMCPPreferences``, so they must exist before
    the first ``import addon.preferences``.
    """
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(
        timers=types.SimpleNamespace(register=lambda *a, **kw: None),
        version=(4, 2, 0),
    )
    bpy.utils = types.SimpleNamespace(resource_path=lambda which: "/tmp/fake-bpy")
    # AddonPreferences stub — needs to be subclass-able.
    class _FakeAddonPreferences:
        pass
    bpy.types = types.SimpleNamespace(AddonPreferences=_FakeAddonPreferences)
    # Property factories return None — fine because the descriptors are
    # only evaluated at class-body time outside Blender.
    bpy.props = types.SimpleNamespace(
        StringProperty=lambda **kw: None,
        BoolProperty=lambda **kw: None,
        EnumProperty=lambda **kw: None,
        IntProperty=lambda **kw: None,
    )
    sys.modules["bpy"] = bpy
    # bpy.props is sometimes imported as `from bpy.props import StringProperty`
    # — that style requires bpy.props to be a real submodule, not just an
    # attribute. Register it in sys.modules too.
    bpy_props = types.ModuleType("bpy.props")
    bpy_props.StringProperty = bpy.props.StringProperty
    bpy_props.BoolProperty = bpy.props.BoolProperty
    bpy_props.EnumProperty = bpy.props.EnumProperty
    bpy_props.IntProperty = bpy.props.IntProperty
    sys.modules["bpy.props"] = bpy_props
    sys.modules["bmesh"] = types.ModuleType("bmesh")
    sys.modules["mathutils"] = types.ModuleType("mathutils")


def main() -> int:
    install_bpy_stubs()

    from addon.client import (
        BlenderMCPClient,
        LOG_LEVEL_TO_PRIORITY,
        MESSAGE_BUS_LOGGER,
    )

    assert MESSAGE_BUS_LOGGER == "_message_bus"
    assert len(LOG_LEVEL_TO_PRIORITY) == 8

    client = BlenderMCPClient(
        "http://example.com/mcp/",
        "fake-jwt",
        "blender-test-uuid",
        executor=object(),
    )

    # Round-trip a bus-shaped notification through the class shim and
    # confirm message_pump enqueues it.
    fake_msg = types.SimpleNamespace(
        method="notifications/message",
        params={
            "logger": MESSAGE_BUS_LOGGER,
            "level": "info",
            "data": (
                '{"target_uuid": "blender-test-uuid", '
                '"payload": {"message_type": "job_dispatch", '
                '"job_id": "j1", "script": "x = 1"}}'
            ),
        },
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client._on_message(fake_msg))
    finally:
        loop.close()

    assert len(client.job_queue) == 1, "bus message did not reach the queue"

    # Phase 8 added BlenderMCPPreferences (transitively imported by the
    # executor). Verify the registry is intact at 24 commands.
    from addon.executor import BlenderCommandExecutor  # noqa: F401
    from addon.executor.registry import COMMAND_REGISTRY

    assert len(COMMAND_REGISTRY) == 24, (
        f"expected 24 registered commands, got {len(COMMAND_REGISTRY)}"
    )

    print("Gate E: PASS — addon.client imports + bus enqueue + 24 registry commands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
