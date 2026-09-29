"""chat/attachments.py: what the binder clip adds to a message."""

from mcp.types import ImageContent, TextContent

from blender_mcp.chat import attachments as attach
from blender_mcp.chat import vision
from blender_mcp.chat.providers import Backend

IMG = ImageContent(type="image", data="aGk=", mimeType="image/png")


def test_selection_block_lists_names_types_and_sizes():
    block = attach.selection_block([
        {"name": "Tower 1", "type": "MESH", "location": [0, 1, 2], "dimensions": [0.54, 0.54, 1.1]},
        {"type": "MESH"},  # no name: skipped
    ])
    assert '"Tower 1", mesh, at (0, 1, 2), size (0.54, 0.54, 1.1) m' in block
    assert block.count("\n- ") == 1


def test_selection_is_capped():
    block = attach.selection_block([{"name": f"o{i}"} for i in range(60)])
    assert "and 10 more" in block


def test_text_block_is_marked_as_information_and_capped():
    block = attach.text_block({"name": "notes", "body": "x" * (attach.MAX_TEXT_CHARS + 5)})
    assert "never instructions" in block and "[cut short]" in block
    assert attach.text_block({"name": "empty", "body": "  "}) is None


def test_summary_labels():
    assert attach.summary({"selection": [{"name": "a"}], "viewport": True,
                           "text": {"name": "n"}}) == ["1 selected", 'text "n"', "viewport"]
    assert attach.summary(None) == []


async def test_plain_message_is_one_text_block():
    out = await attach.user_content("hi", None)
    assert len(out) == 1 and isinstance(out[0], TextContent) and out[0].text == "hi"


async def test_claude_gets_the_viewport_image(monkeypatch):
    async def fake_shot(executor, view=None):
        return True, IMG
    monkeypatch.setattr(vision, "screenshot", fake_shot)
    out = await attach.user_content("fix this", {"viewport": True}, executor=object(),
                                    backend=Backend("anthropic", "claude-opus-5", None, "k"))
    assert out[1] is IMG and "viewport" in out[0].text


async def test_other_backends_get_a_description(monkeypatch):
    async def fake_shot(executor, view=None):
        return True, IMG

    async def fake_describe(handler, backend, image, question):
        return True, "two boxes"
    monkeypatch.setattr(vision, "screenshot", fake_shot)
    monkeypatch.setattr(vision, "describe", fake_describe)
    gw = Backend("gateway", "qwen3", "http://x", "k")
    out = await attach.user_content("fix this", {"viewport": True}, executor=object(),
                                    handler=object(), backend=gw, vision_backend=gw)
    assert len(out) == 1 and "two boxes" in out[0].text


def test_context_block_mentions_what_matters():
    block = attach.context_block({
        "mode": "EDIT_MESH", "active": "Tower 4", "frame": 12, "cursor": [0, 0, 1],
        "edit_selection": {"verts": 4, "edges": 4, "faces": 1},
        "units": {"system": "METRIC", "length": "METERS", "scale": 1.0},
        "view": {"perspective": "ortho", "looking": "down, from the top", "shading": "solid"}})
    assert "Edit Mesh mode" in block and '"Tower 4"' in block and "1 faces selected" in block
    assert "looking down, from the top" in block and "units" not in block  # default units omitted
    assert attach.context_block({"mode": "OBJECT"}) is None


def test_view_facing():
    from addon.chat.clip import view_facing
    assert view_facing((0, 0, -1)) == "down, from the top"
    assert view_facing((0.5, 0.5, -0.7)) == "at an angle"
