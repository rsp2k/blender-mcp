"""Model backends for chat. One call shape for all of them:

    await complete(backend, system, messages, tools, max_tokens=..., timeout_s=...)
        -> Completion(text, tool_calls)

``messages`` are MCP ``SamplingMessage``s and ``tools`` MCP ``Tool``s, as the
sampling handler receives them; each provider converts to its wire format and
back. Failures raise ProviderError with a message safe to show the user (no
keys, no request bodies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("gateway", "anthropic", "openai")


class ProviderError(Exception):
    """A model call failed; ``str(e)`` is the user-facing reason."""


@dataclass(frozen=True)
class Backend:
    """Where one user's model calls go."""

    provider: str  # gateway | anthropic | openai
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)

    def public(self) -> dict:
        return {"provider": self.provider, "model": self.model}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


async def complete(backend: Backend, system: str | None, messages, tools, **kw) -> Completion:
    if backend.provider == "anthropic":
        from .anthropic import complete as run
    else:
        from .openai_compat import complete as run
    return await run(backend, system, messages, tools, **kw)
