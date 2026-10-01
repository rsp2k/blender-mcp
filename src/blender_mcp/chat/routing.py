"""The server's sampling handler: routes ``ctx.sample_step`` to the user's backend.

Installed with ``sampling_handler_behavior="fallback"``: a client that
advertises sampling with tools answers with its own model, and everything
else (the add-on) lands here. ``blender_chat`` resolves the backend once per
turn and puts it in ``current_backend``; a call without one resolves it from
the caller's identity.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import httpx
import httpx2
from mcp.types import CreateMessageResultWithTools, TextContent, ToolUseContent

from .. import instrumentation as qa
from . import settings as chat_settings
from .config import ChatConfig, load_config
from .providers import Backend, Completion, ProviderError, Usage, complete

logger = logging.getLogger(__name__)

current_backend: ContextVar[Backend | None] = ContextVar("chat_current_backend", default=None)
# One chat turn's identity ("<user>:<uuid>"), set by blender_chat next to
# current_backend. Scopes the thinking cache so turns and users never share it.
current_turn: ContextVar[str | None] = ContextVar("chat_current_turn", default=None)
# Set by a chat turn: receives the reply's text as it streams. Only the
# turn's own model calls use it (see __call__), not vision descriptions.
current_text_sink: ContextVar[Any] = ContextVar("chat_current_text_sink", default=None)


class ThinkingCache:
    """Raw Anthropic assistant content per tool round, for replay.

    Keyed by (scope, the round's tool_use ids). The scope is the turn when one
    is set, otherwise a hash of the backend's key, so one account's blocks can
    never be replayed into another's request. Bounded LRU; a finished turn's
    entries are dropped with ``forget``.
    """

    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._data: OrderedDict[tuple[str, frozenset[str]], list[dict]] = OrderedDict()

    @staticmethod
    def scope(backend: Backend) -> str:
        turn = current_turn.get()
        if turn:
            return "turn:" + turn
        digest = hashlib.sha256(f"{backend.base_url}\0{backend.api_key}".encode()).hexdigest()
        return "key:" + digest[:32]

    def put(self, scope: str, ids: frozenset[str], raw: list[dict]) -> None:
        if not ids or not raw:
            return
        key = (scope, ids)
        self._data[key] = raw
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def get(self, scope: str, ids: frozenset[str]) -> list[dict] | None:
        key = (scope, ids)
        raw = self._data.get(key)
        if raw is not None:
            self._data.move_to_end(key)
        return raw

    def forget(self, scope: str) -> None:
        for key in [k for k in self._data if k[0] == scope]:
            del self._data[key]

    def __len__(self) -> int:
        return len(self._data)


def to_result(c: Completion, model: str) -> CreateMessageResultWithTools:
    content: list[Any] = []
    if c.text:
        content.append(TextContent(type="text", text=c.text))
    for call in c.tool_calls:
        content.append(ToolUseContent(type="tool_use", id=call.id, name=call.name,
                                      input=call.arguments))
    if not content:
        content.append(TextContent(type="text", text=""))
    return CreateMessageResultWithTools(
        role="assistant", content=content, model=model,
        stopReason="toolUse" if c.tool_calls else "endTurn",
    )


_USAGE_ATTRS = ("cache_read_input_tokens", "cache_creation_input_tokens",
                "advisor_calls", "advisor_input_tokens", "advisor_output_tokens")


# Bounds for the text given to record_llm_call. The middleware redacts and
# truncates again; these keep the work on the call path small.
PROMPT_MAX_CHARS = 4000
_TOOL_RESULT_CHARS = 300
_TOOL_RESULTS_MAX = 8
# Inline base64 (data URIs, long unbroken runs) never belongs in a record.
_BASE64_RE = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+|[A-Za-z0-9+/]{200,}={0,2}")


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _blocks(content: Any) -> list:
    if content is None:
        return []
    return content if isinstance(content, list) else [content]


def _scrub(text: str) -> str:
    return _BASE64_RE.sub("[base64]", text).strip()


_PLACEHOLDERS = frozenset({"[image]", "[audio]"})


def _block_text(block: Any) -> str:
    """A text block's text; images and audio as a placeholder, never their data."""
    if isinstance(block, str):
        return _scrub(block)
    kind = _field(block, "type")
    if kind == "text":
        return _scrub(str(_field(block, "text") or ""))
    if kind in ("image", "audio"):
        return f"[{kind}]"
    return ""


def _tool_result_text(block: Any) -> str:
    parts = [_block_text(b) for b in _blocks(_field(block, "content"))]
    text = " ".join(p for p in parts if p)
    return text[:_TOOL_RESULT_CHARS]


def prompt_text(messages: Any) -> str | None:
    """What a model call was asked, for ``llm.call``: the user's last turn plus
    a short summary of the newest tool results. Image and audio data are
    dropped, inline base64 is masked, and the whole is cut to PROMPT_MAX_CHARS.
    """
    try:
        user_text = ""
        results: list[str] = []
        for msg in reversed(list(messages or [])):
            if _field(msg, "role") != "user":
                continue
            texts, tool_results = [], []
            for block in _blocks(_field(msg, "content")):
                if _field(block, "type") == "tool_result":
                    tool_results.append(_tool_result_text(block))
                else:
                    texts.append(_block_text(block))
            if not results:
                results = [t for t in tool_results if t][:_TOOL_RESULTS_MAX]
            # The user's own turn: the newest user message with real text.
            if any(t and t not in _PLACEHOLDERS for t in texts):
                user_text = "\n".join(t for t in texts if t)
                break
        lines = [user_text] if user_text else []
        if results:
            lines.append("tool results:")
            lines.extend(f"- {r}" for r in results)
        return "\n".join(lines).strip()[:PROMPT_MAX_CHARS] or None
    except Exception:  # recording must never break a chat turn
        logger.debug("prompt text extraction failed", exc_info=True)
        return None


def record_llm_call(backend: Backend, t0: float, *, usage: Usage | None = None,
                    error: BaseException | None = None, messages: Any = None,
                    completion: str | None = None) -> None:
    """One ``llm.call`` event per completion when QA_LOG is on, keyed by the
    chat turn so a turn's model calls read back together. The event links to
    the enclosing tool call (blender_chat, a vision tool) by itself.

    The prompt (from ``messages``, see prompt_text) and ``completion`` are
    built and passed only when the middleware has capture_llm_text on.
    Never passes the backend's key or base URL. Never raises.
    """
    try:
        mw = qa.middleware()
        if mw is None:
            return
        attrs = {"advisor": backend.advisor or None}
        attrs.update({k: getattr(usage, k, None) for k in _USAGE_ATTRS})
        prompt = None
        if getattr(mw, "capture_llm_text", False):
            prompt = prompt_text(messages)
            completion = _scrub(completion)[:PROMPT_MAX_CHARS] if completion else None
        else:
            completion = None
        mw.record_llm_call(
            model=backend.model,
            provider=backend.provider,
            duration_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            error=error,
            key=current_turn.get(),
            attrs={k: v for k, v in attrs.items() if v is not None},
            prompt=prompt,
            completion=completion or None,
        )
    except Exception:  # recording must never break a chat turn
        logger.debug("llm.call record failed", exc_info=True)


class RoutingSamplingHandler:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        session_factory: Callable[[], Any] | None = None,
        config_loader: Callable[[], ChatConfig] = load_config,
        anthropic_transport: httpx2.AsyncBaseTransport | None = None,
    ):
        # Transports exist for tests; None = network. ``transport`` is an httpx
        # one for the OpenAI-compatible backends, ``anthropic_transport`` an
        # httpx2 one for the Anthropic SDK (which is built on httpx2).
        self.transport = transport
        self.anthropic_transport = anthropic_transport
        self.thinking = ThinkingCache()
        self._session_factory = session_factory
        self.config_loader = config_loader

    @property
    def session_factory(self) -> Callable[[], Any]:
        if self._session_factory is None:
            from ..storage import get_session

            return get_session
        return self._session_factory

    async def load_settings(self, user_sub: str | None):
        """The account's chat_settings row (None without one). ProviderError
        when the database can't be read."""
        if not user_sub:
            return None
        try:
            return await chat_settings.load(self.session_factory, user_sub)
        except Exception as e:
            logger.warning("chat settings lookup failed: %s", type(e).__name__)
            raise ProviderError("could not load this account's chat settings") from e

    async def resolve(self, user_sub: str | None, cfg: ChatConfig | None = None, *,
                      trial_ok: bool = False) -> Backend | None:
        """The user's backend, None when they have none. ProviderError on bad state.

        Trial accounts (CHAT_TRIAL_TURNS) get None unless ``trial_ok``: only
        blender_chat counts their turns, so nothing else may spend them."""
        cfg = cfg or self.config_loader()
        row = await self.load_settings(user_sub)
        return chat_settings.backend_for(row, cfg, user_sub, trial_ok=trial_ok)

    async def complete(self, backend: Backend, system, messages, tools, *,
                       tool_choice: str | None = None, max_tokens: int | None = None,
                       temperature: float | None = None, on_text=None) -> Completion:
        cfg = self.config_loader()
        extra: dict[str, Any] = {}
        scope = None
        if backend.provider == "anthropic":
            scope = self.thinking.scope(backend)
            extra = {
                "anthropic_transport": self.anthropic_transport,
                "replay": lambda ids: self.thinking.get(scope, ids),
                "fallbacks": cfg.anthropic_fallbacks,
                "effort": cfg.anthropic_effort,
                "advisor": backend.advisor,
                "advisor_max_tokens": cfg.anthropic_advisor_max_tokens,
                "advisor_cache": cfg.anthropic_advisor_cache,
                # Only Claude streams: the gateway's streamed tool calls are
                # unreliable (see the gpu gateway notes), so it stays whole.
                "on_text": on_text,
            }
        t0 = time.perf_counter()
        try:
            c = await complete(
                backend, system, messages, tools,
                max_tokens=max_tokens or cfg.max_tokens,
                temperature=temperature,
                tool_choice=tool_choice,
                timeout_s=cfg.llm_timeout_s,
                transport=self.transport,
                **extra,
            )
        except Exception as e:
            record_llm_call(backend, t0, error=e, messages=messages)
            raise
        record_llm_call(backend, t0, usage=c.usage, messages=messages, completion=c.text)
        if scope is not None and c.raw and c.tool_calls:
            self.thinking.put(scope, frozenset(t.id for t in c.tool_calls), c.raw)
        return c

    def end_turn(self, turn: str) -> None:
        self.thinking.forget("turn:" + turn)

    async def __call__(self, messages, params, request_context) -> CreateMessageResultWithTools:
        backend = current_backend.get()
        if backend is None:
            from ..bus_tools import _resolve_user_id

            backend = await self.resolve(_resolve_user_id(None))
            if backend is None:
                raise ProviderError("no chat backend is available for this account")
        c = await self.complete(
            backend, params.systemPrompt, messages, params.tools,
            tool_choice=params.toolChoice.mode if params.toolChoice else None,
            max_tokens=params.maxTokens,
            temperature=params.temperature,
            on_text=current_text_sink.get(),
        )
        return to_result(c, backend.model)
