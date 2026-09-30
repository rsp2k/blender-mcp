"""The add-on's side of streamed replies: deltas grow one message, and the
round's whole text settles it in place instead of adding a copy."""

from addon.chat.state import ChatState


def _turn():
    st = ChatState()
    st.begin_turn("make a crate")
    return st


def assistant(st):
    return [m for m in st.messages if m["role"] == "assistant"]


def test_deltas_grow_one_message_and_text_settles_it():
    st = _turn()
    for piece in ("Made a ", "crate, 1 m ", "on each side."):
        st.apply_event({"t": "delta", "text": piece})
    live = assistant(st)
    assert len(live) == 1 and live[0]["streaming"] and live[0]["text"] == "Made a crate, 1 m on each side."
    st.apply_event({"t": "text", "text": "Made a crate, 1 m on each side."})
    done = assistant(st)
    assert len(done) == 1 and not done[0]["streaming"]


def test_text_before_a_tool_call_is_settled_after_the_tools_run():
    st = _turn()
    st.apply_event({"t": "delta", "text": "Let me measure first."})
    st.apply_event({"t": "tool", "name": "world_bounds", "phase": "start"})
    st.apply_event({"t": "tool", "name": "world_bounds", "phase": "end", "ok": True, "ms": 200})
    # The round's text arrives after its tools; it must not duplicate.
    st.apply_event({"t": "text", "text": "Let me measure first."})
    st.apply_event({"t": "delta", "text": "Done: it's 1 m tall."})
    texts = [m["text"] for m in assistant(st)]
    assert texts == ["Let me measure first.", "Done: it's 1 m tall."]


def test_finishing_the_turn_closes_a_stream_and_keeps_one_copy():
    st = _turn()
    st.apply_event({"t": "delta", "text": "All done."})
    st.finish_turn(st.turn, "assistant", "All done.")
    msgs = assistant(st)
    assert len(msgs) == 1 and not msgs[0]["streaming"]


def test_unstreamed_backends_still_add_text_normally():
    st = _turn()
    st.apply_event({"t": "text", "text": "From the gateway."})
    assert [m["text"] for m in assistant(st)] == ["From the gateway."]
