"""Anthropic Messages API with the user's own key.

The API wants strictly alternating roles starting with ``user``, and tool
definitions on every request whose history holds tool_use blocks, so
consecutive same-role messages are merged and leading assistant turns are
dropped.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from mcp.types import ImageContent, TextContent, ToolResultContent, ToolUseContent

from . import Backend, Completion, ProviderError, ToolCall
from .openai_compat import ATTEMPTS, _error_detail
from .textcalls import normalize_name

ANTHROPIC_VERSION = "2023-06-01"
_RETRY_STATUS = {429, 500, 502, 503, 504, 529}
_CHOICE = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}


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


def to_anthropic_messages(messages) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        content = [x for x in (_block(b) for b in _blocks(m.content)) if x]
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


def parse_response(data: Any, allowed: set[str]) -> Completion:
    texts, calls = [], []
    for c in (data or {}).get("content") or []:
        if c.get("type") == "text" and c.get("text"):
            texts.append(c["text"])
        elif c.get("type") == "tool_use":
            name = normalize_name(c.get("name"), allowed) or str(c.get("name") or "")
            args = c.get("input") if isinstance(c.get("input"), dict) else {}
            calls.append(ToolCall(id=c.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                  name=name, arguments=args))
    return Completion(text="\n".join(texts).strip(), tool_calls=calls)


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
    if not backend.api_key:
        raise ProviderError("no Anthropic API key is saved for this account")
    body: dict[str, Any] = {
        "model": backend.model,
        "max_tokens": max_tokens,
        "messages": to_anthropic_messages(messages),
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t.name, "description": t.description or "",
                          "input_schema": t.inputSchema} for t in tools]
        if tool_choice in _CHOICE:
            body["tool_choice"] = _CHOICE[tool_choice]
    if temperature is not None:
        body["temperature"] = temperature
    headers = {
        "x-api-key": backend.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    allowed = {t.name for t in tools or []}
    url = backend.base_url.rstrip("/") + "/v1/messages"

    last = "the Anthropic API returned an empty response"
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        for _ in range(ATTEMPTS):
            try:
                r = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                last = f"the Anthropic API timed out after {timeout_s:g}s"
                continue
            except httpx.HTTPError as e:
                raise ProviderError(f"could not reach the Anthropic API: {type(e).__name__}") from e
            if r.status_code in _RETRY_STATUS:
                last = f"the Anthropic API returned HTTP {r.status_code}: {_error_detail(r)}"
                continue
            if r.status_code in (401, 403):
                raise ProviderError(f"the Anthropic API rejected the saved key (HTTP {r.status_code})")
            if r.status_code >= 400:
                raise ProviderError(f"the Anthropic API returned HTTP {r.status_code}: {_error_detail(r)}")
            try:
                data = r.json()
            except ValueError:
                last = "the Anthropic API returned a response that is not JSON"
                continue
            result = parse_response(data, allowed)
            if result.text or result.tool_calls:
                return result
    raise ProviderError(last)
