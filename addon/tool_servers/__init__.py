"""Bring Your Own Tools: MCP servers the user adds in the preferences.

    config.py   server specs, name rules, env/token expansion (no bpy)
    results.py  shape a tool result into the user_tool_call reply (no bpy)
    report.py   build the blender_report_user_tools payload (no bpy)
    runner.py   asyncio loop thread holding one client session per server (no bpy)
    bridge.py   the bpy side: prefs snapshots, the drainer hook, reporting

Commands and addresses only ever come from the user's preferences; a
user_tool_call names a server, never a command. The wire contract is in
docs-site/src/content/docs/how-to/tool-servers.mdx.
"""
