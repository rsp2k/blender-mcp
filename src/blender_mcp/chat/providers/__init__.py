"""Model backends for chat. One call shape for all of them:

    await complete(backend, system, messages, tools, max_tokens=..., timeout_s=...)
        -> Completion(text, tool_calls, raw, usage)

``messages`` are MCP ``SamplingMessage``s and ``tools`` MCP ``Tool``s, as the
sampling handler receives them; each provider converts to its wire format and
back. Failures raise ProviderError with a message safe to show the user (no
keys, no request bodies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("gateway", "anthropic", "openai")
# Where the per-turn scene snapshot starts in the system prompt. Everything
# before it is the same on every request, so providers can cache it.
SCENE_MARKER = "\n\nThe scene right now (JSON, may be cut short):\n"
ANTHROPIC_ONLY = ("replay", "fallbacks", "effort")


class ProviderError(Exception):
    """A model call failed; ``str(e)`` is the user-facing reason."""


@dataclass(frozen=True)
class Backend:
    """Where one user's model calls go."""

    provider: str  # gateway | anthropic | openai
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    # Claude only: the model this one may escalate to mid-turn ("" = none).
    advisor: str = ""

    def public(self) -> dict:
        return {"provider": self.provider, "model": self.model}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    """Token counts for one model call, or summed over the requests behind it.

    None means the provider did not report that count. The advisor fields are
    Anthropic only: the escalation model's share, from ``usage.iterations``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    advisor_calls: int | None = None
    advisor_input_tokens: int | None = None
    advisor_output_tokens: int | None = None

    def __add__(self, other: Usage | None) -> Usage:
        if other is None:
            return self
        return Usage(**{f: _add(getattr(self, f), getattr(other, f))
                        for f in self.__dataclass_fields__})


def _add(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else a + b


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-native assistant content (Anthropic: thinking + text + tool_use
    # blocks as returned), for replay in the next round. None when not needed.
    raw: list[dict] | None = field(default=None, repr=False)
    # Tokens this completion cost, summed over retries and continuations.
    usage: Usage | None = None


async def complete(backend: Backend, system: str | None, messages, tools, **kw) -> Completion:
    if backend.provider == "anthropic":
        from .anthropic import complete as run

        kw["transport"] = kw.pop("anthropic_transport", None)
    else:
        from .openai_compat import complete as run

        kw.pop("anthropic_transport", None)
        for k in ANTHROPIC_ONLY:
            kw.pop(k, None)
    return await run(backend, system, messages, tools, **kw)
