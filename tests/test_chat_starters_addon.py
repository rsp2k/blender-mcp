"""Add-on side of the Chat panel's starter prompts: parsing prompts/list,
picking a group, reading prompts/get, and filling the message field without
sending. Pure Python, no bpy."""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
)

from addon.chat import client as chat_client
from addon.chat import compose
from addon.chat import state as chat_mod
from addon.chat.starters import first_user_text, parse_starters, pick_starters
from addon.chat.state import ChatState


def starter(name, title, group, order=None, **extra):
    meta = {"blender_mcp": {"starter": True, "group": group}}
    if order is not None:
        meta["blender_mcp"]["order"] = order
    meta["fastmcp"] = {"tags": []}  # FastMCP's own key rides alongside ours
    return Prompt(name=name, title=title, description=f"About {title}.",
                  arguments=[], _meta=meta, **extra)


SERVER_PROMPTS = [
    Prompt(name="blender_dispatch_recipe", description="recipes",
           arguments=[PromptArgument(name="command", required=True)]),
    starter("blender_starter_describe_scene", "Describe this scene", "general", 4),
    starter("blender_starter_b_wave", "Make B wave", "b_clip", 1),
    starter("blender_starter_b_top_hat", "Give B a top hat", "b_clip", 2),
    starter("blender_starter_table_chair", "Add a table and chair", "general", 5),
]


def done(result=None, exc=None):
    fut = Future()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
    return fut


@pytest.fixture
def fresh_state(monkeypatch):
    st = ChatState()
    monkeypatch.setattr(chat_mod, "chat_state", st)
    monkeypatch.setattr(chat_client, "chat_state", st)
    monkeypatch.setattr(compose, "chat_state", st)
    monkeypatch.setattr(chat_client, "request_redraw", lambda: None)
    return st


# --- parsing prompts/list -------------------------------------------------------

def test_parse_keeps_only_flagged_starters_in_order():
    found = parse_starters(SERVER_PROMPTS)
    assert [s["name"] for s in found] == [
        "blender_starter_b_wave", "blender_starter_b_top_hat",
        "blender_starter_describe_scene", "blender_starter_table_chair",
    ]
    wave = found[0]
    assert wave == {"name": "blender_starter_b_wave", "title": "Make B wave",
                    "description": "About Make B wave.", "group": "b_clip", "order": 1}


def test_old_server_without_meta_has_no_starters():
    old = [Prompt(name="blender_dispatch_recipe", description="x"),
           Prompt(name="blender_feedback_help", description="y")]
    assert parse_starters(old) == []
    assert parse_starters([]) == []
    assert parse_starters(None) == []


def test_meta_without_the_flag_or_from_someone_else_is_ignored():
    prompts = [
        Prompt(name="a", _meta={"blender_mcp": {"group": "general"}}),
        Prompt(name="b", _meta={"blender_mcp": {"starter": "yes"}}),
        Prompt(name="c", _meta={"other": {"starter": True}}),
    ]
    assert parse_starters(prompts) == []


def test_starter_that_needs_arguments_is_skipped():
    p = starter("blender_starter_x", "X", "general")
    p.arguments = [PromptArgument(name="what", required=True)]
    assert parse_starters([p]) == []


def test_dict_shaped_prompts_and_missing_title():
    raw = [{"name": "blender_starter_light_scene", "_meta": {
        "blender_mcp": {"starter": True, "group": "general"}}}]
    (s,) = parse_starters(raw)
    assert s["title"] == "Light scene" and s["group"] == "general" and s["order"] is None


def test_unordered_starters_follow_ordered_ones_in_server_order():
    found = parse_starters([
        starter("u1", "U1", "general"),
        starter("o2", "O2", "general", 2),
        starter("u2", "U2", "general"),
        starter("o1", "O1", "general", 1),
    ])
    assert [s["name"] for s in found] == ["o1", "o2", "u1", "u2"]


def test_list_prompts_result_object_is_accepted():
    found = parse_starters(SimpleNamespace(prompts=SERVER_PROMPTS))
    assert len(found) == 4


# --- group selection ------------------------------------------------------------

def test_b_clip_group_in_the_template_or_with_b_in_the_scene():
    found = parse_starters(SERVER_PROMPTS)
    b = ["Make B wave", "Give B a top hat"]
    general = ["Describe this scene", "Add a table and chair"]
    assert [s["title"] for s in pick_starters(found, "B_Clip", False)] == b
    assert [s["title"] for s in pick_starters(found, "", True)] == b
    assert [s["title"] for s in pick_starters(found, "", False)] == general
    assert [s["title"] for s in pick_starters(found, "2D_Animation", False)] == general


def test_b_clip_falls_back_to_general_when_the_server_has_none():
    general_only = parse_starters([starter("g", "G", "general", 1)])
    assert [s["name"] for s in pick_starters(general_only, "B_Clip", True)] == ["g"]
    assert pick_starters([], "B_Clip", True) == []
    assert pick_starters(None) == []


# --- prompts/get ----------------------------------------------------------------

def test_first_user_text_from_get_prompt():
    result = GetPromptResult(messages=[
        PromptMessage(role="assistant", content=TextContent(type="text", text="no")),
        PromptMessage(role="user", content=TextContent(type="text", text="  Make B wave.  ")),
    ])
    assert first_user_text(result) == "Make B wave."
    assert first_user_text(GetPromptResult(messages=[])) is None
    assert first_user_text(None) is None


# --- the message field: fill without sending -------------------------------------

class FakeWM:
    """Assigning the field fires the update hook synchronously, as RNA does."""

    def __init__(self, sent):
        object.__setattr__(self, "blendermcp_chat_input", "")
        object.__setattr__(self, "_sent", sent)

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)
        if key == "blendermcp_chat_input":
            compose.on_input_changed(self, self._send)

    def _send(self, text):
        self._sent.append(text)
        return True, None


def test_fill_does_not_send_and_leaves_the_text(fresh_state):
    sent = []
    wm = FakeWM(sent)
    compose.fill(wm, "Make B. Clip wave hello.")
    assert sent == []
    assert wm.blendermcp_chat_input == "Make B. Clip wave hello."


def test_a_real_edit_after_a_fill_still_sends(fresh_state):
    sent = []
    wm = FakeWM(sent)
    compose.fill(wm, "Make B. Clip wave hello.")
    # The user edits and presses Enter: that's an ordinary assignment.
    wm.blendermcp_chat_input = "Make B. Clip wave hello twice."
    assert sent == ["Make B. Clip wave hello twice."]
    assert wm.blendermcp_chat_input == ""  # cleared after a send


def test_enter_on_the_unedited_fill_sends(fresh_state):
    sent = []
    wm = FakeWM(sent)
    compose.fill(wm, "Describe this scene.")
    wm.blendermcp_chat_input = "Describe this scene."  # confirm without edits
    assert sent == ["Describe this scene."]


def test_guard_does_not_linger_if_the_hook_never_fired(fresh_state):
    wm = SimpleNamespace(blendermcp_chat_input="")  # no hook at all
    compose.fill(wm, "Light this scene nicely.")
    assert wm.blendermcp_chat_input == "Light this scene nicely."
    assert not fresh_state.take_prefill("Light this scene nicely.")


def test_hook_sends_normally_without_a_guard(fresh_state):
    sent = []
    wm = SimpleNamespace(blendermcp_chat_input="  add a cube ")
    compose.on_input_changed(wm, lambda t: (sent.append(t), (True, None))[1])
    assert sent == ["add a cube"] and wm.blendermcp_chat_input == ""
    wm.blendermcp_chat_input = ""
    compose.on_input_changed(wm, lambda t: (sent.append(t), (True, None))[1])
    assert sent == ["add a cube"]  # clearing is a no-op


def test_unsent_message_stays_in_the_field(fresh_state):
    wm = SimpleNamespace(blendermcp_chat_input="add a cube")
    compose.on_input_changed(wm, lambda t: (False, "Not connected."))
    assert wm.blendermcp_chat_input == "add a cube"


# --- client: listing and using starters ---------------------------------------------

def test_listed_starters_land_in_state(fresh_state):
    chat_client._on_starters_listed(done(SERVER_PROMPTS))
    assert [s["title"] for s in fresh_state.snapshot()["starters"]] == [
        "Make B wave", "Give B a top hat", "Describe this scene", "Add a table and chair"]


def test_list_failure_means_no_starters_not_an_error(fresh_state):
    fresh_state.set_starters([{"name": "stale"}])
    chat_client._on_starters_listed(done(exc=RuntimeError("Method not found")))
    assert fresh_state.starters == [] and fresh_state.last_error is None


def test_starter_text_is_filled_for_the_latest_click_only(fresh_state, monkeypatch):
    filled = []
    monkeypatch.setattr(chat_client, "_schedule_fill",
                        lambda text, click: chat_client._fill_input(text, click))
    monkeypatch.setattr(compose, "fill", lambda wm, text: filled.append(text))
    import sys
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(
        context=SimpleNamespace(window_manager=object())))
    first = fresh_state.next_starter_click()
    second = fresh_state.next_starter_click()
    result = GetPromptResult(messages=[
        PromptMessage(role="user", content=TextContent(type="text", text="Give B a hat."))])
    chat_client._on_starter_prompt(done(result), first)  # superseded
    assert filled == []
    chat_client._on_starter_prompt(done(result), second)
    assert filled == ["Give B a hat."]


def test_get_prompt_failure_is_reported(fresh_state):
    click = fresh_state.next_starter_click()
    chat_client._on_starter_prompt(done(exc=RuntimeError("Unknown prompt")), click)
    assert "Unknown prompt" in fresh_state.last_error


def test_use_starter_when_not_connected(fresh_state, monkeypatch):
    monkeypatch.setattr(chat_client, "_live_client", lambda: None)
    assert "Not connected" in chat_client.use_starter("blender_starter_b_wave")
    assert fresh_state.starter_click == 0


def test_registration_fetches_starters_with_chat(fresh_state, monkeypatch):
    calls = []
    monkeypatch.setattr(chat_client, "refresh_backend", lambda: calls.append("backend"))
    monkeypatch.setattr(chat_client, "refresh_starters", lambda: calls.append("starters"))
    chat_client.on_registered({"features": ["chat"]})
    assert calls == ["backend", "starters"]
    calls.clear()
    chat_client.on_registered({"features": []})
    assert calls == []
