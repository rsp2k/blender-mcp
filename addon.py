# Code created by Siddharth Ahuja: www.github.com/ahujasid
# Transformed for FastMCP OAuth Message Bus integration
#
# This file is a thin shim around the `addon/` package, preserving
# Blender's "Install Add-on" -> single-file path. All real code lives
# under addon/. The 9-phase modularization refactor that produced this
# layout is documented in the project's git history (commit
# 8651fdb..HEAD on branch refactor/addon-modularize).
#
# bl_info MUST be a literal dict at module scope (Blender parses it
# without importing the module). It mirrors addon/__init__.py:bl_info;
# the two are kept in sync manually + verified by a grep check.

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (1, 5, 4),  # MUST match addon/_version.py:tuple_version
    "blender": (3, 2, 0),  # uses bpy.context.temp_override (3.2+)
    "location": "View3D > Sidebar > BlenderMCP",
    "description": (
        "Connect Blender to the BlenderMCP server as a bus client. "
        "Requires fastmcp: <blender_python> -m pip install fastmcp"
    ),
    "category": "Interface",
}

from addon import register, unregister  # noqa: E402,F401

if __name__ == "__main__":
    register()
