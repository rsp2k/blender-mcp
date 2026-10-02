"""The add-on's side of the free trial and the Chat panel's key prompt:
trial counts from every result, the needs-key flag, and Save key."""

import json
import sys
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from addon.chat import client as chat_client
from addon.chat.state import (
    ChatState,
    key_box_changes_key,
    key_prompt_heading,
    normalize_trial,
    own_key_row_text,
    result_message,
    trial_exhausted,
    trial_low,
    trial_row_text,
    uses_own_key,
)

TRIAL = {"unit": "usd", "limit": 0.5, "used": 0.1234, "remaining": 0.3766, "percent_left": 75}
ENDED = {"unit": "usd", "limit": 0.5, "used": 0.5, "remaining": 0.0, "percent_left": 0}
# Servers from before cost budgets counted messages, with no "unit".
OLD_TRIAL = {"limit": 5, "used": 2, "remaining": 3}
OLD_ENDED = {"limit": 5, "used": 5, "remaining": 0}
BACKEND = {"provider": "anthropic", "model": "claude-sonnet-5", "has_key": True, "source": "user"}
SERVER_BACKEND = {"provider": "gateway", "model": "qwen3", "has_key": False, "source": "server"}
HINT = "Your free messages are used up. Paste your Claude API key in the Chat panel to keep going."


def finish(st, payload):
    st.begin_turn("add a cube")
    role, text = result_message(payload)
    assert st.finish_turn(st.turn, role, text, payload)
    return role, text


def text_result(obj, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(obj))], data=None,
                           is_error=is_error)


def done(result=None, exc=None):
    fut = Future()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
    return fut


# --- trial from each result type ----------------------------------------------

def test_trial_from_get_backend():
    st = ChatState()
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": TRIAL})
    assert st.trial == TRIAL and st.backend == BACKEND
    assert st.snapshot()["trial"] == TRIAL


def test_trial_from_chat_results_ok_and_error():
    st = ChatState()
    finish(st, {"status": "ok", "reply": "Added.", "trial": TRIAL})
    assert st.trial == TRIAL
    finish(st, {"status": "backend_error", "detail": "HTTP 502",
                "trial": {**TRIAL, "used": 0.3, "remaining": 0.2, "percent_left": 40}})
    assert st.trial["percent_left"] == 40


def test_unlimited_account_clears_trial():
    st = ChatState()
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": TRIAL})
    finish(st, {"status": "ok", "reply": "hi", "trial": None})
    assert st.trial is None


@pytest.mark.parametrize("raw", [
    None, "5", {"limit": 5}, {"limit": 5, "used": 1, "remaining": "4"},
    {"limit": True, "used": 0, "remaining": 1},
    {"unit": "usd", "limit": 0.5, "used": "x", "remaining": 0.1, "percent_left": 80},
])
def test_malformed_trial_reads_as_none(raw):
    assert normalize_trial(raw) is None


def test_budget_shape_kept_and_percent_bounded():
    assert normalize_trial(TRIAL) == TRIAL
    assert normalize_trial({**TRIAL, "percent_left": 140})["percent_left"] == 100
    assert normalize_trial({**TRIAL, "percent_left": -3})["percent_left"] == 0
    # Missing percent: worked out from remaining / limit.
    no_pct = {k: v for k, v in TRIAL.items() if k != "percent_left"}
    assert normalize_trial(no_pct)["percent_left"] == 75


def test_old_message_shape_is_tagged_and_never_negative():
    assert normalize_trial(OLD_TRIAL) == {"unit": "messages", **OLD_TRIAL}
    assert normalize_trial({"limit": 5, "used": 6, "remaining": -1})["remaining"] == 0


@pytest.mark.parametrize("trial,exhausted", [
    (TRIAL, False), (ENDED, True),
    ({**TRIAL, "remaining": 0.002, "percent_left": 0}, True),   # rounds to 0%
    ({**TRIAL, "remaining": -0.01, "percent_left": 3}, True),   # overspent by the last turn
    (OLD_TRIAL, False), (OLD_ENDED, True), (None, False),
])
def test_trial_exhausted(trial, exhausted):
    assert trial_exhausted(normalize_trial(trial)) is exhausted


@pytest.mark.parametrize("trial,low", [
    ({**TRIAL, "percent_left": 16}, False), ({**TRIAL, "percent_left": 15}, True),
    ({**OLD_TRIAL, "remaining": 4}, False), ({**OLD_TRIAL, "remaining": 3}, True),
])
def test_trial_low_threshold(trial, low):
    assert trial_low(normalize_trial(trial)) is low


# --- older servers (no "trial" field) -------------------------------------------

def test_old_server_get_backend_means_no_trial():
    st = ChatState()
    st.trial = dict(TRIAL)
    st.apply_backend_result({"status": "ok", "backend": BACKEND})
    assert st.trial is None


def test_old_server_chat_result_leaves_trial_unset():
    st = ChatState()
    finish(st, {"status": "ok", "reply": "hi"})
    assert st.trial is None and st.needs_key is None
    assert key_prompt_heading(st.snapshot()) is None


def test_advisor_answer_without_trial_keeps_it():
    st = ChatState()
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": TRIAL})
    st.apply_backend_result({"status": "ok", "backend": BACKEND}, keep_trial=True)
    assert st.trial == TRIAL


# --- needs-key ------------------------------------------------------------------

def test_trial_ended_sets_needs_key_and_reads_well():
    st = ChatState()
    role, text = finish(st, {"status": "trial_ended", "trial": ENDED, "hint": HINT})
    assert st.needs_key == "trial_ended" and st.trial == ENDED
    # A note, not an error: nothing failed. Short, because the key box
    # right below carries the instructions the server's hint repeats.
    assert role == "status" and text == "Your free trial is used up."
    assert st.last_error is None
    snap = st.snapshot()
    assert snap["needs_key"] == "trial_ended"
    assert key_prompt_heading(snap) == "Paste your Claude API key to keep chatting"


def test_trial_ended_note_is_the_same_with_or_without_hint():
    role, text = result_message({"status": "trial_ended"})
    assert role == "status"
    assert text == "Your free trial is used up."
    assert "—" not in text and "!" not in text


def test_no_backend_sets_needs_key_with_start_heading():
    st = ChatState()
    finish(st, {"status": "no_backend", "hint": "Add a key to start."})
    assert st.needs_key == "no_backend"
    assert key_prompt_heading(st.snapshot()) == "Paste your Claude API key to start chatting"


def test_ok_reply_clears_a_stale_needs_key():
    st = ChatState()
    st.needs_key = "trial_ended"
    finish(st, {"status": "ok", "reply": "hi", "trial": None})
    assert st.needs_key is None


def test_saved_key_clears_needs_key():
    st = ChatState()
    finish(st, {"status": "trial_ended", "trial": ENDED, "hint": HINT})
    st.begin_key_check()
    assert st.snapshot()["key_checking"]
    assert st.finish_key_check({"status": "ok", "backend": BACKEND, "trial": None})
    snap = st.snapshot()
    assert snap["needs_key"] is None and snap["trial"] is None
    assert snap["backend"] == BACKEND and not snap["key_checking"]
    assert key_prompt_heading(snap) is None


@pytest.mark.parametrize("code", ["invalid_api_key", "key_check_failed"])
def test_rejected_key_shows_detail_and_keeps_the_box(code):
    st = ChatState()
    finish(st, {"status": "trial_ended", "trial": ENDED, "hint": HINT})
    st.begin_key_check()
    assert not st.finish_key_check({"status": "error", "error": code, "detail": "Anthropic said 401."})
    snap = st.snapshot()
    assert snap["key_error"] == "Anthropic said 401."
    assert snap["needs_key"] == "trial_ended" and snap["trial"] == ENDED
    assert key_prompt_heading(snap) is not None


def test_rejected_key_without_detail_gets_plain_text():
    st = ChatState()
    st.finish_key_check({"status": "error", "error": "invalid_api_key"})
    assert "wasn't accepted" in st.key_error


def test_preferences_save_also_ends_the_prompt():
    st = ChatState()
    st.needs_key = "no_backend"
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": None}, saved=True)
    assert st.needs_key is None


def test_refresh_alone_does_not_end_the_prompt():
    st = ChatState()
    st.needs_key = "no_backend"
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": None})
    assert st.needs_key == "no_backend"


def test_backend_error_path_unchanged():
    st = ChatState()
    st.apply_backend_result({"status": "error", "error": "invalid_api_key", "detail": "Bad key."},
                            saved=True)
    assert st.backend_error == "Bad key." and st.backend is None


# --- the box and the trial row ----------------------------------------------------

def test_box_shows_when_trial_hits_zero_without_a_refusal():
    st = ChatState()
    finish(st, {"status": "ok", "reply": "Last one.", "trial": ENDED})
    snap = st.snapshot()
    assert snap["needs_key"] is None
    assert key_prompt_heading(snap) == "Paste your Claude API key to keep chatting"


def test_use_my_own_key_toggles_the_box():
    st = ChatState()
    st.trial = dict(TRIAL)
    assert key_prompt_heading(st.snapshot()) is None
    assert st.toggle_key_prompt()
    assert key_prompt_heading(st.snapshot()) == "Paste your Claude API key"
    assert not st.toggle_key_prompt()
    assert key_prompt_heading(st.snapshot()) is None


def test_budget_row_shows_percent_and_shortens():
    trial = normalize_trial(TRIAL)
    assert trial_row_text(trial, 60) == ("Free trial: 75% left", "Use my own key")
    assert trial_row_text(trial, 35) == ("75% free left", "Use my own key")
    assert trial_row_text(trial, 31) == ("75% free left", "Use my key")
    assert trial_row_text(trial, 30) == ("75% left", "Use my own key")
    assert trial_row_text(trial, 10) == ("75% left", "Use my key")
    for chars in range(5, 80):
        label, button = trial_row_text(trial, chars)
        # A percentage only: never money or tokens.
        assert "75%" in label and "$" not in label and "0.37" not in label
        assert "token" not in label and button.startswith("Use my")


def test_old_server_row_still_counts_messages():
    trial = normalize_trial({"limit": 15, "used": 3, "remaining": 12})
    assert trial_row_text(trial, 60) == ("12 free messages left", "Use my own key")
    assert trial_row_text(normalize_trial({"limit": 5, "used": 4, "remaining": 1}), 60) == \
        ("1 free message left", "Use my own key")
    for chars in range(5, 60):
        label, button = trial_row_text(trial, chars)
        assert label.startswith("12 free") and button.startswith("Use my")
    assert trial_row_text(trial, 20) == ("12 free left", "Use my key")


def test_old_server_zero_messages_opens_the_box():
    st = ChatState()
    finish(st, {"status": "ok", "reply": "Last one.", "trial": OLD_ENDED})
    assert key_prompt_heading(st.snapshot()) == "Paste your Claude API key to keep chatting"


def test_save_with_budget_still_empty_keeps_the_prompt():
    st = ChatState()
    st.needs_key = "trial_ended"
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": ENDED}, saved=True)
    assert st.needs_key == "trial_ended"


def test_new_account_forgets_key_state():
    st = ChatState()
    st.trial, st.needs_key, st.key_error = dict(ENDED), "trial_ended", "Bad key."
    st.reset_account()
    assert (st.trial, st.needs_key, st.key_error) == (None, None, None)


# --- client callbacks ---------------------------------------------------------------

@pytest.fixture
def fresh_state(monkeypatch):
    st = ChatState()
    monkeypatch.setattr(chat_client, "chat_state", st)
    monkeypatch.setattr(chat_client, "request_redraw", lambda: None)
    return st


def test_send_refused_while_a_key_is_needed(fresh_state, monkeypatch):
    # preferences.py needs bpy; send_problem only wants the login token from it.
    prefs = SimpleNamespace(get_prefs=lambda: SimpleNamespace(jwt_token="t"))
    monkeypatch.setitem(sys.modules, "addon.preferences", prefs)
    monkeypatch.setattr(chat_client, "_live_client", lambda: object())
    fresh_state.available = True
    fresh_state.needs_key = "trial_ended"
    assert "API key" in chat_client.send_problem()


def test_key_result_callback_success(fresh_state):
    fresh_state.needs_key = "trial_ended"
    fresh_state.begin_key_check()
    chat_client._on_key_result(done(text_result({"status": "ok", "backend": BACKEND, "trial": None})))
    assert fresh_state.needs_key is None and fresh_state.backend == BACKEND
    assert not fresh_state.key_checking


def test_key_result_callback_failure_and_exception(fresh_state):
    fresh_state.begin_key_check()
    chat_client._on_key_result(done(text_result(
        {"status": "error", "error": "invalid_api_key", "detail": "That key was refused."})))
    assert fresh_state.key_error == "That key was refused."
    fresh_state.begin_key_check()
    chat_client._on_key_result(done(exc=TimeoutError()))
    assert "Couldn't reach the server" in fresh_state.key_error and not fresh_state.key_checking


def test_save_key_when_not_connected(fresh_state, monkeypatch):
    monkeypatch.setattr(chat_client, "_live_client", lambda: None)
    assert chat_client.save_key("sk-ant-x") == "Not connected."
    assert fresh_state.key_error == "Not connected." and not fresh_state.key_checking
    assert chat_client.save_key("   ") == "Paste a key first."


def test_save_key_sends_provider_and_key_only(fresh_state, monkeypatch):
    calls = []

    def fake_start(name, args, on_done):
        calls.append((name, args, on_done))

    monkeypatch.setattr(chat_client, "_start_backend_call", fake_start)
    assert chat_client.save_key("  sk-ant-abc  ") is None
    assert fresh_state.key_checking
    assert calls == [("blender_set_chat_backend", {"provider": "anthropic", "api_key": "sk-ant-abc"},
                      chat_client._on_key_result)]


def test_backend_callback_marks_set_calls_as_saves(fresh_state):
    fresh_state.needs_key = "no_backend"
    ok = text_result({"status": "ok", "backend": BACKEND, "trial": None})
    chat_client._on_backend_result(done(ok), chat_client.GET_BACKEND_TOOL)
    assert fresh_state.needs_key == "no_backend"
    chat_client._on_backend_result(done(ok), chat_client.SET_BACKEND_TOOL)
    assert fresh_state.needs_key is None


# --- changing or removing the account's own key ---------------------------------

@pytest.mark.parametrize("backend, own", [
    (BACKEND, True),
    (None, False),
    (SERVER_BACKEND, False),
    # A server default on Claude is still the server's key, not the account's.
    ({"provider": "anthropic", "model": "claude-opus-5", "has_key": True, "source": "server"}, False),
    ({"provider": "openai", "model": "m", "has_key": True, "source": "user"}, False),
    ({**BACKEND, "has_key": False}, False),
])
def test_uses_own_key(backend, own):
    assert uses_own_key({"backend": backend}) is own


def own_key_state():
    st = ChatState()
    st.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": None})
    return st


def test_change_key_opens_the_same_box_with_its_own_heading():
    st = own_key_state()
    snap = st.snapshot()
    assert key_prompt_heading(snap) is None and not key_box_changes_key(snap)
    assert st.toggle_key_prompt()
    snap = st.snapshot()
    assert key_prompt_heading(snap) == "Paste a new Claude API key"
    assert key_box_changes_key(snap)
    assert not st.toggle_key_prompt()
    assert key_prompt_heading(st.snapshot()) is None


def test_trial_box_is_not_a_change():
    st = ChatState()
    st.apply_backend_result({"status": "ok", "backend": SERVER_BACKEND, "trial": TRIAL})
    st.toggle_key_prompt()
    snap = st.snapshot()
    assert key_prompt_heading(snap) == "Paste your Claude API key"
    assert not key_box_changes_key(snap)


def test_new_key_saved_closes_the_change_box():
    st = own_key_state()
    st.toggle_key_prompt()
    st.begin_key_check()
    assert key_prompt_heading(st.snapshot()) == "Paste a new Claude API key"
    assert st.finish_key_check({"status": "ok", "backend": BACKEND, "trial": None})
    snap = st.snapshot()
    assert key_prompt_heading(snap) is None and uses_own_key(snap)


def test_refused_new_key_keeps_the_box_and_the_old_key():
    st = own_key_state()
    st.toggle_key_prompt()
    st.begin_key_check()
    assert not st.finish_key_check({"status": "error", "error": "invalid_api_key", "trial": None})
    snap = st.snapshot()
    assert "wasn't accepted" in snap["key_error"]
    assert key_box_changes_key(snap) and snap["backend"] == BACKEND


def test_cancel_closes_the_box_with_nothing_changed():
    st = own_key_state()
    st.toggle_key_prompt()
    st.arm_key_remove()
    st.key_error = "Bad key."
    st.close_key_prompt()
    snap = st.snapshot()
    assert key_prompt_heading(snap) is None
    assert not snap["key_remove_armed"] and snap["key_error"] is None
    assert snap["backend"] == BACKEND


def test_remove_asks_first_and_keep_backs_out():
    st = own_key_state()
    st.toggle_key_prompt()
    st.arm_key_remove()
    assert st.snapshot()["key_remove_armed"]
    st.arm_key_remove(False)
    snap = st.snapshot()
    assert not snap["key_remove_armed"] and key_box_changes_key(snap)
    # Closing the box by its toggle also forgets a pending confirm.
    st.arm_key_remove()
    st.toggle_key_prompt()
    st.toggle_key_prompt()
    assert not st.snapshot()["key_remove_armed"]


def test_remove_cannot_be_armed_while_a_key_is_checked():
    st = own_key_state()
    st.toggle_key_prompt()
    st.begin_key_check()
    st.arm_key_remove()
    assert not st.snapshot()["key_remove_armed"]


def test_removed_key_closes_the_box_and_brings_the_trial_back():
    st = own_key_state()
    st.toggle_key_prompt()
    st.arm_key_remove()
    st.begin_key_remove()
    snap = st.snapshot()
    assert snap["key_checking"] and snap["key_removing"] and not snap["key_remove_armed"]
    assert st.finish_key_remove({"status": "ok", "backend": SERVER_BACKEND, "trial": TRIAL})
    snap = st.snapshot()
    assert not uses_own_key(snap) and snap["trial"] == TRIAL
    assert not snap["key_checking"] and not snap["key_removing"]
    assert key_prompt_heading(snap) is None


def test_removed_key_with_trial_used_up_asks_for_a_key_again():
    st = own_key_state()
    st.toggle_key_prompt()
    st.begin_key_remove()
    assert st.finish_key_remove({"status": "ok", "backend": SERVER_BACKEND, "trial": ENDED})
    snap = st.snapshot()
    assert key_prompt_heading(snap) == "Paste your Claude API key to keep chatting"
    assert not key_box_changes_key(snap)


@pytest.mark.parametrize("payload, error, shown", [
    ({"status": "error", "error": "unauthenticated"}, None,
     "Couldn't remove the key. Try again in a moment."),
    ({"status": "error", "error": "x", "detail": "Database is down."}, None,
     "Couldn't remove the key: Database is down."),
    (None, "Not connected.", "Not connected."),
])
def test_failed_remove_keeps_the_key_and_says_why(payload, error, shown):
    st = own_key_state()
    st.toggle_key_prompt()
    st.begin_key_remove()
    assert not st.finish_key_remove(payload, error=error)
    snap = st.snapshot()
    assert snap["key_error"] == shown and snap["backend"] == BACKEND
    assert key_box_changes_key(snap) and not snap["key_checking"]


def test_new_account_forgets_a_pending_remove():
    st = own_key_state()
    st.toggle_key_prompt()
    st.arm_key_remove()
    st.reset_account()
    assert not st.key_remove_armed


def test_own_key_row_shortens():
    assert own_key_row_text(60) == ("Using your Claude key", "Change key")
    assert own_key_row_text(39) == ("Using your Claude key", "Change key")
    assert own_key_row_text(38) == ("Your Claude key", "Change key")
    assert own_key_row_text(26) == ("Your key", "Change key")
    assert own_key_row_text(25) == ("Your key", "Change")
    for chars in range(80):
        label, button = own_key_row_text(chars)
        assert "key" in label.lower() and button.startswith("Change")
        assert "\u2014" not in label + button


def test_remove_key_sends_clear_only(fresh_state, monkeypatch):
    calls = []
    monkeypatch.setattr(chat_client, "_start_backend_call",
                        lambda name, args, on_done: calls.append((name, args, on_done)))
    assert chat_client.remove_key() is None
    assert fresh_state.key_checking and fresh_state.key_removing
    assert calls == [("blender_set_chat_backend", {"provider": "anthropic", "clear": True},
                      chat_client._on_remove_result)]
    # A second click while the first is out sends nothing.
    assert chat_client.remove_key() is not None and len(calls) == 1


def test_remove_key_when_not_connected(fresh_state, monkeypatch):
    monkeypatch.setattr(chat_client, "_live_client", lambda: None)
    assert chat_client.remove_key() == "Not connected."
    assert fresh_state.key_error == "Not connected."
    assert not fresh_state.key_checking and not fresh_state.key_removing


def test_remove_result_callback(fresh_state):
    fresh_state.apply_backend_result({"status": "ok", "backend": BACKEND, "trial": None})
    fresh_state.key_prompt_requested = True
    fresh_state.begin_key_remove()
    chat_client._on_remove_result(done(exc=TimeoutError()))
    assert "Couldn't reach the server" in fresh_state.key_error
    assert fresh_state.backend == BACKEND
    fresh_state.begin_key_remove()
    refused = SimpleNamespace(content=[SimpleNamespace(text="denied")], data=None, is_error=True)
    chat_client._on_remove_result(done(refused))
    assert fresh_state.key_error == "denied" and fresh_state.key_prompt_requested
    fresh_state.begin_key_remove()
    chat_client._on_remove_result(done(text_result(
        {"status": "ok", "backend": SERVER_BACKEND, "trial": TRIAL})))
    assert fresh_state.backend == SERVER_BACKEND and fresh_state.trial == TRIAL
    assert not fresh_state.key_prompt_requested and fresh_state.key_error is None
