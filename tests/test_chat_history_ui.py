"""Chat tab history: timings, transcript rows, saved conversations."""

from addon.chat import history
from addon.chat.state import ChatState, format_duration, tool_line, transcript_rows


def test_durations_are_decimal_seconds():
    assert format_duration(307) == "0.31 s"
    assert format_duration(4315) == "4.32 s"
    assert format_duration(85415) == "1m 25s"
    assert format_duration(None) == ""


def test_tool_line_reports_the_wait_for_the_user_apart():
    assert tool_line({"name": "set_view", "ms": 307}) == "set_view  0.31 s"
    assert tool_line({"name": "execute_code", "ms": 4300, "wait_ms": 81200}) == \
        "execute_code  4.30 s · waited 1m 21s for you"
    assert "waited" not in tool_line({"name": "x", "ms": 10, "wait_ms": 200})


def test_transcript_rows_wrap_mark_speakers_and_end_with_a_spacer():
    msgs = [{"role": "user", "text": "hello there"},
            {"role": "tool", "name": "create_mesh", "ok": True, "ms": 390},
            {"role": "assistant", "text": "one two"}]
    rows = transcript_rows(msgs, lambda t: t.split())  # one word per line
    assert [r["role"] for r in rows] == ["user", "user", "tool", "assistant", "assistant", "spacer"]
    assert rows[0]["icon"] == "USER" and rows[1]["icon"] == "BLANK1"
    assert rows[2] == {"role": "tool", "text": "create_mesh  0.39 s", "icon": "CHECKMARK"}
    assert rows[3]["icon"] == "MONKEY"


def test_wait_ms_lands_on_the_tool_entry():
    st = ChatState()
    st.begin_turn("go")
    st.apply_event({"t": "tool", "name": "execute_code", "phase": "start"})
    st.apply_event({"t": "tool", "name": "execute_code", "phase": "end", "ok": True, "ms": 4300, "wait_ms": 81200})
    tool = [m for m in st.messages if m["role"] == "tool"][0]
    assert tool["wait_ms"] == 81200 and tool["ok"] is True


def test_saved_chats_round_trip_and_prune(tmp_path):
    msgs = [{"role": "user", "text": "Make a table top please " * 3, "turn": 1},
            {"role": "assistant", "text": "Done.", "turn": 1}]
    history.save(tmp_path, "a", msgs)
    assert history.load(tmp_path, "a") == msgs
    (listed,) = history.listing(tmp_path)
    assert listed["id"] == "a" and listed["title"].endswith("…") and len(listed["title"]) == history.TITLE_CHARS
    for i in range(history.KEEP + 3):
        history.save(tmp_path, f"c{i:02d}", msgs)
    assert len(history.listing(tmp_path)) == history.KEEP
    history.save(tmp_path, "empty", [])  # nothing to save, nothing written
    assert history.load(tmp_path, "empty") == []


def test_loading_a_conversation_restores_turns_and_is_refused_mid_turn():
    st = ChatState()
    st.load_conversation("x", [{"role": "user", "text": "hi", "turn": 3},
                               {"role": "tool", "name": "t", "ok": None, "turn": 3}])
    assert st.conversation_id == "x" and st.turn == 3
    assert st.messages[1]["ok"] is False  # an unfinished step from last time reads as failed
    st.begin_turn("again")
    assert st.load_conversation("y", []) is False
