"""Claude through the official ``anthropic`` SDK, with the user's (or the server's) key.

MCP sampling messages carry text, images, tool_use and tool_result only, so
each round's assistant turn comes back without its thinking blocks. The
routing handler keeps the raw assistant content of every tool round
(``Completion.raw``) and hands it back through ``replay``: an assistant
message whose tool_use ids match a cached response is sent as those raw
blocks, unchanged, so adaptive thinking carries across tool rounds.

The API wants roles alternating from ``user``, so consecutive same-role
messages are merged and leading assistant turns are dropped.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
from mcp.types import ImageContent, TextContent, ToolResultContent, ToolUseContent

from . import Backend, Completion, ProviderError, ToolCall
from .textcalls import normalize_name

logger = logging.getLogger(__name__)

# Non-streaming calls: 16000 leaves room for adaptive thinking plus the answer
# and stays under the SDK's non-streaming timeout guard.
MIN_MAX_TOKENS = 16000
MAX_RETRIES = 2  # the SDK retries 408/409/429/5xx and connection errors
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that take ``fallbacks: "default"`` (refusals re-run server-side).
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")
# Models that 400 on forced tool use (tool_choice any/tool).
NO_FORCED_TOOLS = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5")
# Models that still accept temperature/top_p/top_k. Everything else in the
# current family (Opus 4.7+, Sonnet 5, Fable, Mythos) rejects them, and an
# unknown future claude-* model is assumed to as well.
SAMPLING_OK = ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-5",
               "claude-sonnet-4-5", "claude-opus-4-1", "claude-opus-4-0", "claude-sonnet-4-0",
               "claude-3")
EMPTY_ATTEMPTS = 2

Replay = Callable[[frozenset[str]], list[dict] | None]


def _base(model: str) -> str:
    return (model or "").strip().lower()


def _matches(model: str, names: tuple[str, ...]) -> bool:
    m = _base(model)
    # Exact id, or a dated/Vertex snapshot of it; "claude-opus-5-5" is not "claude-opus-5".
    return any(m == n or m.startswith(n + "-20") or m.startswith(n + "@") for n in names)


def allows_sampling(model: str) -> bool:
    m = _base(model)
    return not m.startswith("claude") or any(m.startswith(p) for p in SAMPLING_OK)


def allows_forced_tools(model: str) -> bool:
    return not _matches(model, NO_FORCED_TOOLS)


def supports_fallbacks(model: str) -> bool:
    return _matches(model, FALLBACK_MODELS)


def tool_choice_for(mode: str | None, model: str) -> dict | None:
    if mode == "auto":
        return {"type": "auto"}
    if mode == "none":
        return {"type": "none"}
    if mode == "required":
        return {"type": "any"} if allows_forced_tools(model) else {"type": "auto"}
    return None


# ---- request conversion ------------------------------------------------------

def _blocks(content) -> list:
    return content if isinstance(content, list) else [content]


def _block(b) -> dict | None:
    if isinstance(b, TextContent):
        return {"type": "text", "text": b.text} if b.text else None
    if isinstance(b, ImageContent):
        return {"type": "image",
                "source": {"type": "base64", "media_type": b.mimeType, "data": b.data}}
    if isinstance(b, ToolUseContent):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultContent):
        inner = [x for x in (_block(c) for c in b.content or []) if x]
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": b.toolUseId,
                               "content": inner or [{"type": "text", "text": "(no output)"}]}
        if b.isError:
            out["is_error"] = True
        return out
    return None


def _tool_use_ids(blocks) -> frozenset[str]:
    return frozenset(b.id for b in blocks if isinstance(b, ToolUseContent))


def to_anthropic_messages(messages, replay: Replay | None = None) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        blocks = _blocks(m.content)
        content = None
        if replay is not None and m.role == "assistant":
            ids = _tool_use_ids(blocks)
            raw = replay(ids) if ids else None
            if raw:
                content = [dict(b) for b in raw]
        if content is None:
            content = [x for x in (_block(b) for b in blocks) if x]
        if not content:
            continue
        if not out and m.role != "user":
            continue
        if out and out[-1]["role"] == m.role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": m.role, "content": content})
    for msg in out:
        # tool_result blocks must lead a user message.
        if msg["role"] == "user":
            msg["content"].sort(key=lambda c: c["type"] != "tool_result")
    return out


def strip_thinking(messages: list[dict]) -> list[dict]:
    return [{**m, "content": [c for c in m["content"]
                              if c.get("type") not in ("thinking", "redacted_thinking")]}
            for m in messages]


def build_request(backend: Backend, system: str | None, messages: list[dict], tools, *,
                  max_tokens: int, temperature: float | None, tool_choice: str | None,
                  fallbacks: bool, effort: str) -> dict[str, Any]:
    req: dict[str, Any] = {
        "model": backend.model,
        "max_tokens": max(MIN_MAX_TOKENS, max_tokens or 0),
        "messages": messages,
    }
    if system:
        req["system"] = system
    if tools:
        req["tools"] = [{"name": t.name, "description": t.description or "",
                         "input_schema": t.inputSchema} for t in tools]
        choice = tool_choice_for(tool_choice, backend.model)
        if choice:
            req["tool_choice"] = choice
    if effort:
        req["output_config"] = {"effort": effort}
    extra: dict[str, Any] = {}
    if temperature is not None and allows_sampling(backend.model):
        # Not in the SDK 1.x signature; only older models accept it at all.
        extra["temperature"] = temperature
    if fallbacks and supports_fallbacks(backend.model):
        req["betas"] = [FALLBACK_BETA]
        req["fallbacks"] = "default"
    if extra:
        req["extra_body"] = extra
    return req


# ---- response ----------------------------------------------------------------

def _dump(block) -> dict:
    if isinstance(block, dict):
        return dict(block)
    return block.to_dict(mode="json")


def raw_content(message) -> list[dict]:
    """The assistant content to replay: every block exactly as returned, minus
    ``fallback`` markers (an ignored audit block)."""
    return [d for d in (_dump(b) for b in getattr(message, "content", None) or [])
            if d.get("type") != "fallback"]


def refusal_message(message) -> str:
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details is not None else None
    explanation = getattr(details, "explanation", None) if details is not None else None
    text = "Claude declined this request"
    text += f" (refusal category: {category})" if category else " (refusal)"
    if explanation:
        text += f": {str(explanation)[:300]}"
    return text + ". Try rephrasing it, or pick another model."


def parse_message(message, allowed: set[str]) -> Completion:
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        raise ProviderError(refusal_message(message))
    raw = raw_content(message)
    texts, calls = [], []
    for c in raw:
        if c.get("type") == "text" and c.get("text"):
            texts.append(c["text"])
        elif c.get("type") == "tool_use":
            name = normalize_name(c.get("name"), allowed) or str(c.get("name") or "")
            args = c.get("input") if isinstance(c.get("input"), dict) else {}
            calls.append(ToolCall(id=c.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                  name=name, arguments=args))
    text = "\n".join(texts).strip()
    if stop == "max_tokens" and calls:
        # The last tool input may be cut off; don't run any of this turn's tools.
        calls, raw = [], []
        if not text:
            raise ProviderError("Claude ran out of output tokens before finishing its tool call")
    return Completion(text=text, tool_calls=calls, raw=raw if calls else None)


# ---- errors ------------------------------------------------------------------

def _api_message(e: anthropic.APIStatusError) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(getattr(e, "message", "") or e)[:300]


def _is_thinking_binding_error(e: anthropic.BadRequestError) -> bool:
    msg = _api_message(e).lower()
    return "thinking" in msg and "signature" in msg


def _translate(e: Exception, backend: Backend, timeout_s: float) -> ProviderError:
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ProviderError(f"the Anthropic API rejected the saved key (HTTP {e.status_code})")
    if isinstance(e, anthropic.NotFoundError):
        return ProviderError(f"the Anthropic API does not know the model {backend.model!r}: "
                             f"{_api_message(e)}")
    if isinstance(e, anthropic.BadRequestError):
        return ProviderError(f"the Anthropic API refused the request: {_api_message(e)}")
    if isinstance(e, anthropic.RateLimitError):
        return ProviderError("the Anthropic API is rate-limiting this key (HTTP 429); "
                             "try again shortly")
    if isinstance(e, anthropic.APIStatusError):
        return ProviderError(f"the Anthropic API returned HTTP {e.status_code}: {_api_message(e)}")
    if isinstance(e, anthropic.APITimeoutError):
        return ProviderError(f"the Anthropic API timed out after {timeout_s:g}s")
    if isinstance(e, anthropic.APIConnectionError):
        return ProviderError("could not reach the Anthropic API")
    return ProviderError(f"the Anthropic API call failed: {type(e).__name__}")


# ---- the call ----------------------------------------------------------------

def make_client(backend: Backend, timeout_s: float,
                transport: httpx2.AsyncBaseTransport | None = None) -> anthropic.AsyncAnthropic:
    http_client = anthropic.DefaultAsyncHttpxClient(transport=transport) if transport else None
    return anthropic.AsyncAnthropic(api_key=backend.api_key, base_url=backend.base_url,
                                    timeout=timeout_s, max_retries=MAX_RETRIES,
                                    http_client=http_client)


async def complete(
    backend: Backend,
    system: str | None,
    messages,
    tools,
    *,
    max_tokens: int = MIN_MAX_TOKENS,
    temperature: float | None = None,
    tool_choice: str | None = None,
    timeout_s: float = 120,
    transport: httpx2.AsyncBaseTransport | None = None,
    replay: Replay | None = None,
    fallbacks: bool = True,
    effort: str = "",
    **_ignored: Any,
) -> Completion:
    """``transport`` is an httpx2 transport for tests (httpx2.MockTransport)."""
    if not backend.api_key:
        raise ProviderError("no Anthropic API key is saved for this account")
    wire = to_anthropic_messages(messages, replay)
    allowed = {t.name for t in tools or []}

    def request(msgs: list[dict]) -> dict[str, Any]:
        return build_request(backend, system, msgs, tools, max_tokens=max_tokens,
                             temperature=temperature, tool_choice=tool_choice,
                             fallbacks=fallbacks, effort=effort)

    async with make_client(backend, timeout_s, transport) as client:
        stripped = False
        attempts = 0
        while True:
            attempts += 1
            try:
                message = await client.beta.messages.create(**request(wire))
            except anthropic.BadRequestError as e:
                # A replayed thinking block the API won't accept (history no
                # longer matches it): answer this round without the thinking.
                if not stripped and _is_thinking_binding_error(e):
                    logger.info("anthropic: replayed thinking rejected; retrying without it")
                    wire, stripped = strip_thinking(wire), True
                    continue
                raise _translate(e, backend, timeout_s) from e
            except anthropic.AnthropicError as e:
                raise _translate(e, backend, timeout_s) from e
            result = parse_message(message, allowed)
            if result.text or result.tool_calls:
                return result
            if attempts >= EMPTY_ATTEMPTS:
                raise ProviderError("the Anthropic API returned an empty response")
