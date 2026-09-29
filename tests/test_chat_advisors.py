"""Escalation: which advisor a turn gets, and what an account may choose."""

import pytest

from blender_mcp.chat import advisors
from blender_mcp.chat import settings as chat_settings
from blender_mcp.chat.config import ChatConfig, load_config
from blender_mcp.chat.settings import SettingsError


def test_haiku_can_escalate_to_everything_above_it():
    opts = advisors.allowed_for("claude-haiku-4-5-20251001")
    assert opts[0] == "claude-mythos-5-1" and "claude-opus-5" in opts
    assert "claude-haiku-4-5" not in opts and len(opts) == 12


def test_top_models_and_self_advice():
    assert advisors.allowed_for("claude-fable-5-1") == ["claude-mythos-5-1"]
    assert "claude-opus-5" not in advisors.allowed_for("claude-opus-5")
    assert "claude-opus-4-8" not in advisors.allowed_for("claude-opus-5")  # API refuses it
    assert advisors.allowed_for("qwen3") == []


def test_allowlist_narrows_the_options():
    allow = advisors.parse_allowlist("claude-opus-5, claude-sonnet-5")
    assert advisors.allowed_for("claude-haiku-4-5", allow) == ["claude-opus-5", "claude-sonnet-5"]


def test_effective_follows_choice_then_server_then_off():
    assert advisors.effective("claude-sonnet-5", None, "claude-opus-5") == "claude-opus-5"
    assert advisors.effective("claude-sonnet-5", "claude-fable-5-1", "claude-opus-5") == "claude-fable-5-1"
    assert advisors.effective("claude-sonnet-5", "off", "claude-opus-5") == ""
    # A choice the executor can't use (after a model change) quietly turns off.
    assert advisors.effective("claude-opus-5", "claude-sonnet-5", "") == ""


def test_config_reads_the_allowlist():
    cfg = load_config({"CHAT_ANTHROPIC_ADVISORS": "claude-opus-5,claude-fable-5-1"})
    assert cfg.anthropic_advisors == frozenset({"claude-opus-5", "claude-fable-5-1"})


def test_view_lists_options_for_the_server_model():
    cfg = ChatConfig(default_provider="anthropic", default_model="claude-haiku-4-5",
                     anthropic_api_key="k", anthropic_advisor="claude-opus-5")
    view = chat_settings.public_view(None, cfg)
    assert view["advisor"] == "claude-opus-5" and view["advisor_choice"] is None
    assert "claude-mythos-5-1" in view["advisors"]


def test_backend_carries_the_advisor():
    cfg = ChatConfig(default_provider="anthropic", default_model="claude-sonnet-5",
                     anthropic_api_key="k", anthropic_advisor="claude-opus-5")
    assert chat_settings.backend_for(None, cfg, "u").advisor == "claude-opus-5"


def test_invalid_choice_is_refused():
    cfg = ChatConfig(default_provider="anthropic", default_model="claude-opus-5",
                     anthropic_api_key="k")
    with pytest.raises(SettingsError, match="can't escalate"):
        chat_settings.check_advisor("claude-sonnet-5", None, cfg)
    assert chat_settings.check_advisor("off", None, cfg) == "off"
    assert chat_settings.check_advisor("", None, cfg) is None
