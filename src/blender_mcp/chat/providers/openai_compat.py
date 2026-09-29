"""OpenAI-style ``/chat/completions``: the GPU gateway, Ollama, LiteLLM, vLLM.

Non-streaming (LiteLLM's streamed tool calls are unreliable). Control flow
branches on the payload, never on ``finish_reason``: the gateway has reported
``stop`` for tool calls and for truncated turns alike. An empty or
unparseable answer is retried once, then reported.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
from mcp.types import ImageContent, TextContent, ToolResultContent, ToolUseContent

from . import Backend, Completion, ProviderError, ToolCall, Usage
from .textcalls import normalize_name, parse_arguments, recover

ATTEMPTS = 2
_RETRY_STATUS = {429, 500, 502, 503, 504}
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
INVALID_ARGS = "__invalid_arguments__"


def _blocks(content) -> list:
    return content if isinstance(content, list) else [content]


def _result_text(block: ToolResultContent) -> str:
    parts = []
    for c in block.content or []:
        if isinstance(c, TextContent):
            parts.append(c.text)
        elif isinstance(c, ImageContent):
            parts.append("[image]")
    return "\n".join(parts)


def to_openai_messages(system: str | None, messages) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        blocks = _blocks(m.content)
        if m.role == "assistant":
            text = "\n".join(b.text for b in blocks if isinstance(b, TextContent))
            calls = [{
                "id": b.id, "type": "function",
                "function": {"name": b.name, "arguments": json.dumps(b.input)},
            } for b in blocks if isinstance(b, ToolUseContent)]
            msg: dict[str, Any] = {"role": "assistant", "content": text or (None if calls else "")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        parts: list[dict] = []
        for b in blocks:
            if isinstance(b, ToolResultContent):
                out.append({"role": "tool", "tool_call_id": b.toolUseId, "content": _result_text(b)})
            elif isinstance(b, TextContent):
                parts.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageContent):
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{b.mimeType};base64,{b.data}"}})
        if parts:
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def to_openai_tools(tools) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description or "", "parameters": t.inputSchema,
    }} for t in tools or []]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_of(data: Any) -> Usage | None:
    """The response's ``usage``: prompt/completion tokens, plus cached prompt
    tokens when the server reports ``prompt_tokens_details.cached_tokens``."""
    u = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    details = u.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return Usage(input_tokens=_int(u.get("prompt_tokens")),
                 output_tokens=_int(u.get("completion_tokens")),
                 cache_read_input_tokens=_int(cached))


def parse_response(data: Any, allowed: set[str]) -> Completion:
    usage = usage_of(data)
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return Completion(usage=usage)
    text = _THINK_RE.sub("", _content_text(msg.get("content"))).strip()
    calls: list[ToolCall] = []
    for raw in msg.get("tool_calls") or []:
        fn = (raw or {}).get("function") or {}
        name = normalize_name(fn.get("name"), allowed) or str(fn.get("name") or "")
        if not name:
            continue
        args = parse_arguments(fn.get("arguments"))
        if args is None:
            args = {INVALID_ARGS: str(fn.get("arguments"))[:500]}
        calls.append(ToolCall(id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                              name=name, arguments=args))
    if not calls and text:
        calls, text = recover(text, allowed)
    return Completion(text=text, tool_calls=calls, usage=usage)


def _error_detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        msg = err.get("message", err) if isinstance(err, dict) else err
        return str(msg)[:300]
    except ValueError:
        return r.text[:300]


async def complete(
    backend: Backend,
    system: str | None,
    messages,
    tools,
    *,
    max_tokens: int = 2048,
    temperature: float | None = None,
    tool_choice: str | None = None,
    timeout_s: float = 120,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Completion:
    label = "the GPU gateway" if backend.provider == "gateway" else "the model endpoint"
    body: dict[str, Any] = {
        "model": backend.model,
        "messages": to_openai_messages(system, messages),
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = to_openai_tools(tools)
        if tool_choice:
            body["tool_choice"] = tool_choice
    if temperature is not None:
        body["temperature"] = temperature
    headers = {"Content-Type": "application/json"}
    if backend.api_key:
        headers["Authorization"] = f"Bearer {backend.api_key}"
    allowed = {t.name for t in tools or []}
    url = backend.base_url.rstrip("/") + "/chat/completions"

    last = f"{label} returned an empty response"
    spent: Usage | None = None  # an empty answer that gets retried is still billed
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        for _ in range(ATTEMPTS):
            try:
                r = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                last = f"{label} timed out after {timeout_s:g}s"
                continue
            except httpx.HTTPError as e:
                raise ProviderError(f"could not reach {label}: {type(e).__name__}") from e
            if r.status_code in _RETRY_STATUS:
                last = f"{label} returned HTTP {r.status_code}: {_error_detail(r)}"
                continue
            if r.status_code >= 400:
                raise ProviderError(f"{label} returned HTTP {r.status_code}: {_error_detail(r)}")
            try:
                data = r.json()
            except ValueError:
                last = f"{label} returned a response that is not JSON"
                continue
            result = parse_response(data, allowed)
            spent = result.usage if spent is None else spent + result.usage
            result.usage = spent
            if result.text or result.tool_calls:
                return result
            last = f"{label} returned an empty response"
    raise ProviderError(last)
