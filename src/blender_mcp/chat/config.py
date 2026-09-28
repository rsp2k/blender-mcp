"""Chat settings from the environment. Read per call, so tests can patch env.

Every variable has a default here and a matching ``${VAR:-default}`` in
docker-compose.yml; an empty string counts as unset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_GPU_BASE_URL = "https://blender-chat.gpu.supported.systems/v1"
DEFAULT_MODEL = "qwen3"
DEFAULT_VISION_MODEL = "qwen2.5vl"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def _get(env, name: str, default: str = "") -> str:
    return (env.get(name) or "").strip() or default


def _int(env, name: str, default: int, lo: int = 1, hi: int | None = None) -> int:
    try:
        v = int(_get(env, name) or default)
    except ValueError:
        v = default
    v = max(lo, v)
    return min(v, hi) if hi is not None else v


def _csv(env, name: str) -> frozenset[str]:
    return frozenset(p.strip() for p in _get(env, name).split(",") if p.strip())


@dataclass(frozen=True)
class ChatConfig:
    enabled: bool = False
    gpu_base_url: str = DEFAULT_GPU_BASE_URL
    gpu_api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    max_steps: int = 10
    max_tokens: int = 2048
    llm_timeout_s: int = 120
    turn_timeout_s: int = 300
    approval_timeout_s: int = 120
    # Blender tools plus the add-on's own tool servers, offered per turn.
    max_tools: int = 40
    # Empty = the curated default list in catalog.py.
    tools: frozenset[str] = frozenset()
    # Empty = every authenticated user may use the shared gateway.
    gateway_users: frozenset[str] = frozenset()
    secret_key: str = field(default="", repr=False)

    def gateway_allowed(self, user_sub: str | None) -> bool:
        if not self.gpu_api_key or not user_sub:
            return False
        return not self.gateway_users or user_sub in self.gateway_users


def load_config(env=None) -> ChatConfig:
    env = os.environ if env is None else env
    return ChatConfig(
        enabled=_get(env, "CHAT_ENABLED").lower() in ("1", "true", "yes", "on"),
        gpu_base_url=_get(env, "GPU_BASE_URL", DEFAULT_GPU_BASE_URL).rstrip("/"),
        gpu_api_key=_get(env, "GPU_API_KEY"),
        model=_get(env, "CHAT_MODEL", DEFAULT_MODEL),
        vision_model=_get(env, "CHAT_VISION_MODEL", DEFAULT_VISION_MODEL),
        max_steps=_int(env, "CHAT_MAX_STEPS", 10, hi=50),
        max_tokens=_int(env, "CHAT_MAX_TOKENS", 2048, lo=64),
        llm_timeout_s=_int(env, "CHAT_LLM_TIMEOUT_S", 120),
        turn_timeout_s=_int(env, "CHAT_TURN_TIMEOUT_S", 300),
        approval_timeout_s=_int(env, "CHAT_APPROVAL_TIMEOUT_S", 120),
        max_tools=_int(env, "CHAT_MAX_TOOLS", 40, hi=128),
        tools=_csv(env, "CHAT_TOOLS"),
        gateway_users=_csv(env, "CHAT_GATEWAY_USERS"),
        secret_key=_get(env, "CHAT_SECRET_KEY"),
    )
