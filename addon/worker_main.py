"""Entry script for a background worker: ``blender -b snapshot.blend --python worker_main.py``.

Imports the Blender MCP addon (by the module name the parent passed in
BLENDER_MCP_ADDON_MODULE) and hands control to its worker loop. When the
loop returns, this script ends and background Blender exits.
"""

import importlib
import os
import sys

module_name = os.environ.get("BLENDER_MCP_ADDON_MODULE", "")
if not module_name:
    print("[BlenderMCP] Worker: BLENDER_MCP_ADDON_MODULE is not set; exiting")
    sys.exit(2)

try:
    addon = importlib.import_module(module_name)
    worker = importlib.import_module(module_name + ".worker")
except Exception as e:  # noqa: BLE001 - report and exit
    print(f"[BlenderMCP] Worker: cannot import {module_name}: {e}")
    sys.exit(2)

reason = worker.run_worker_loop()
print(f"[BlenderMCP] Worker finished: {reason}")
