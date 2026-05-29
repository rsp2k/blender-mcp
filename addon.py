# Code created by Siddharth Ahuja: www.github.com/ahujasid
# Transformed for FastMCP OAuth Message Bus integration
#
# This file is a thin shim around the `addon/` package, preserving
# Blender's "Install Add-on" -> single-file path. All real code lives
# under addon/. The 9-phase modularization refactor that produced this
# layout is documented in the project's git history (commit
# 8651fdb..HEAD on branch refactor/addon-modularize).
#
# bl_info MUST be a literal dict at module scope. Blender's addon
# enumeration uses ast.literal_eval on the right-hand side of the
# bl_info assignment to populate the Preferences > Add-ons list WITHOUT
# importing the module (safety: don't run arbitrary code to list
# addons). literal_eval only accepts pure data literals — imported
# names like `tuple_version` raise ValueError. Verified by writing a
# 5-line probe; see commit history for the experiment. Bottom line:
# the version tuple has to be a literal here, mirrored from
# addon/__init__.py and addon/_version.py. Use scripts/bump_addon_version.py
# to bump all three atomically.

bl_info = {
    "name": "Blender MCP",
    "author": "BlenderMCP",
    "version": (1, 5, 7),  # MUST match addon/_version.py:tuple_version
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
