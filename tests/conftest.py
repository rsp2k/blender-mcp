# test_connection_scenarios.py targets the legacy socket server
# (src/blender_mcp/server.py) and 7 of its 9 tests depend on a `client`
# fixture that was never defined, so it can't run as a suite.
collect_ignore = ["test_connection_scenarios.py"]
