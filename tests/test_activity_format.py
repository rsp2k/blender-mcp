"""Labels for the sidebar Activity list and the status-bar indicator."""

from addon.activity_format import format_ago, format_ms, pending_prompt


def test_durations_read_naturally():
    assert format_ms(412) == "412 ms"
    assert format_ms(2400) == "2.4 s"
    assert format_ms(180_000) == "3 min"


def test_ages_round_down_to_the_unit():
    assert format_ago(-3) == "0s ago"
    assert format_ago(42) == "42s ago"
    assert format_ago(125) == "2m ago"
    assert format_ago(7300) == "2h ago"


def test_prompt_priority_and_quiet_default():
    assert pending_prompt(None, None, None, None) is None
    assert pending_prompt({"job_id": 1}, None, None, {"x": 1}) == "control request"
    assert pending_prompt({"job_id": 1}, None, None, None, chat_approval=True) == "approval waiting"
    assert pending_prompt(None, None, {"p": 1}, {"m": 1}) == "merge offer"
