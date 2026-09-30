"""A Messages API reply as server-sent events, for faking streamed requests.

The anthropic SDK parses these itself, so a test that streams through it
checks our use of the real stream parser, not a hand-made stand-in.
"""

from __future__ import annotations

import json


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _chunks(text: str, size: int = 7) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def message_to_sse(message: dict) -> str:
    """``message`` is a complete (non-streamed) Messages API reply."""
    start = {**message, "content": [], "stop_reason": None,
             "usage": {**message.get("usage", {}), "output_tokens": 0}}
    out = [_event("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(message.get("content", [])):
        kind = block.get("type")
        if kind == "text":
            out.append(_event("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {"type": "text", "text": ""}}))
            for piece in _chunks(block["text"]):
                out.append(_event("content_block_delta", {"type": "content_block_delta", "index": i,
                                                          "delta": {"type": "text_delta", "text": piece}}))
        elif kind == "tool_use":
            out.append(_event("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {**block, "input": {}}}))
            out.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        elif kind == "thinking":
            out.append(_event("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": {"type": "thinking", "thinking": "",
                                                                        "signature": ""}}))
            out.append(_event("content_block_delta", {"type": "content_block_delta", "index": i,
                                                      "delta": {"type": "signature_delta",
                                                                "signature": block.get("signature", "")}}))
        else:
            # Server tool blocks (server_tool_use, advisor_tool_result) arrive whole.
            out.append(_event("content_block_start", {"type": "content_block_start", "index": i,
                                                      "content_block": block}))
        out.append(_event("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
        "usage": {"output_tokens": message.get("usage", {}).get("output_tokens", 0)}}))
    out.append(_event("message_stop", {"type": "message_stop"}))
    return "".join(out)
