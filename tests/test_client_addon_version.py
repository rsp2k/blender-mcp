"""addon_version on ClientInfo: stored, exposed, and kept across bare re-registers."""

from blender_mcp.message_bus import ClientInfo, MessageBus


def test_addon_version_exposed_and_kept():
    bus = MessageBus("b1")
    bus.register(ClientInfo(uuid="blender-1", client_type="blender", addon_version="2026.926.8"))
    assert bus.get("blender-1").to_dict()["addon_version"] == "2026.926.8"
    bus.register(ClientInfo(uuid="blender-1", client_type="blender"))
    assert bus.get("blender-1").addon_version == "2026.926.8"
    bus.register(ClientInfo(uuid="blender-1", client_type="blender", addon_version="2026.926.9"))
    assert bus.get("blender-1").addon_version == "2026.926.9"


def test_old_clients_omit_addon_version():
    bus = MessageBus("b1")
    bus.register(ClientInfo(uuid="blender-2", client_type="blender"))
    assert "addon_version" not in bus.get("blender-2").to_dict()
