"""addon/chat/reader.py: the conversation as a readable document."""

from addon.chat import clip, reader

MESSAGES = [
    {"role": "user", "text": "What's in this scene?", "turn": 1, "at": 0,
     "clipped": ["3 selected"]},
    {"role": "tool", "name": "list_scene_objects", "ok": True, "ms": 190, "turn": 1},
    {"role": "tool", "name": "look_at_viewport", "ok": False, "ms": 2000,
     "error": "no storage", "turn": 1},
    {"role": "assistant", "text": "Eight objects. The label sits *below* the floor, "
                                  "at **z = -0.32**.", "turn": 1},
]


def test_plain_strips_markdown_but_keeps_maths():
    assert reader.plain("**bold** and *soft* and `code`") == "bold and soft and code"
    assert reader.plain("2 * 3 * 4") == "2 * 3 * 4"
    assert reader.plain("## Heading\ntext") == "Heading\ntext"


def test_model_label():
    assert reader.model_label({"model": "claude-opus-5"}) == "Claude Opus 5"
    assert reader.model_label({"model": "claude-sonnet-5-5"}) == "Claude Sonnet 5.5"
    assert reader.model_label({"model": "qwen3"}) == "qwen3"
    assert reader.model_label(None) == ""


def test_document_has_a_header_per_turn_steps_and_clean_reply():
    doc = reader.render_document(MESSAGES, {"model": "claude-opus-5"})
    assert "── You" in doc and "(clipped: 3 selected)" in doc
    assert "── Claude · 2 steps · 2.19 s" in doc
    assert "✓ list_scene_objects" in doc
    assert "✗ look_at_viewport  2.00 s: no storage" in doc
    assert "The label sits below the floor, at z = -0.32." in doc
    assert "*" not in doc.split("── Claude")[1]


def test_turns_group_by_user_message():
    t = reader.turns(MESSAGES + [{"role": "user", "text": "next", "turn": 2}])
    assert len(t) == 2 and len(t[0]["steps"]) == 2 and t[1]["user"]["text"] == "next"


def test_preview_cuts_long_replies():
    wrap = lambda s: [s[i:i + 10] for i in range(0, len(s), 10)]  # noqa: E731
    lines, cut = reader.preview("x" * 100, wrap, max_lines=3)
    assert len(lines) == 3 and cut
    lines, cut = reader.preview("short", wrap, max_lines=3)
    assert lines == ["short"] and not cut


def test_clip_labels():
    assert clip.labels(3, True, "notes") == ["3 selected", "viewport", 'text "notes"']
    assert clip.labels(0, False, "") == []


def test_reading_starts_at_the_newest_turn():
    doc = reader.render_document(MESSAGES + [{"role": "user", "text": "again", "turn": 2}])
    i = reader.latest_turn_line(doc)
    assert doc.split("\n")[i].startswith("── You") and doc.split("\n")[i + 1] == "again"
