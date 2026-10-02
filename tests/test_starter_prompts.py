"""Starter prompts as served over MCP: titles, meta and the one message."""

from fastmcp import Client, FastMCP

from blender_mcp.starter_prompts import STARTERS, register_starter_prompts


def server() -> FastMCP:
    mcp = FastMCP("t")
    register_starter_prompts(mcp, prefix="blender")
    return mcp


async def test_list_prompts_carries_title_and_starter_meta():
    async with Client(server()) as c:
        prompts = {p.name: p for p in await c.list_prompts()}
    assert len(prompts) == len(STARTERS)
    for order, s in enumerate(STARTERS, start=1):
        p = prompts[f"blender_{s.name}"]
        assert p.title == s.title
        assert p.description == s.description
        assert p.arguments in (None, [])
        # FastMCP adds its own "fastmcp" key beside ours; ours arrives intact.
        assert p.meta["blender_mcp"] == {"starter": True, "group": s.group, "order": order}


async def test_meta_on_the_wire_is_underscore_meta():
    async with Client(server()) as c:
        raw = await c.list_prompts_mcp()
    wire = raw.model_dump(by_alias=True, exclude_none=True)["prompts"][0]
    assert wire["_meta"]["blender_mcp"]["starter"] is True


async def test_get_prompt_returns_one_user_message_with_the_text():
    async with Client(server()) as c:
        for s in STARTERS:
            result = await c.get_prompt(f"blender_{s.name}")
            assert len(result.messages) == 1
            msg = result.messages[0]
            assert msg.role == "user"
            assert msg.content.type == "text" and msg.content.text == s.text


def test_starters_are_stable_and_grouped():
    names = [s.name for s in STARTERS]
    assert len(set(names)) == len(names)
    assert all(n.startswith("starter_") for n in names)
    assert {s.group for s in STARTERS} == {"b_clip", "general"}
    for s in STARTERS:
        assert len(s.title) <= 28, s.title  # fits a sidebar button
        assert "—" not in s.text + s.title + s.description


def test_prompts_reference_lists_every_starter_verbatim():
    from pathlib import Path
    doc = (Path(__file__).parents[1]
           / "docs-site/src/content/docs/reference/prompts.mdx").read_text()
    for s in STARTERS:
        assert f"| `blender_{s.name}` | {s.title} | `{s.group}` | {s.text} |" in doc, s.name


async def test_registers_beside_the_prompts_component():
    from blender_mcp.prompts_component import BlenderPromptsComponent
    mcp = FastMCP("t")
    BlenderPromptsComponent().register_prompts(mcp_server=mcp, prefix="blender")
    register_starter_prompts(mcp, prefix="blender")
    async with Client(mcp) as c:
        prompts = await c.list_prompts()
    names = {p.name for p in prompts}
    assert {"blender_dispatch_recipe", "blender_starter_b_wave"} <= names
    # Only the starters carry the starter flag.
    flagged = {p.name for p in prompts
               if ((p.meta or {}).get("blender_mcp") or {}).get("starter")}
    assert flagged == {f"blender_{s.name}" for s in STARTERS}

