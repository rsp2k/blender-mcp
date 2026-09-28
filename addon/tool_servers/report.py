"""The blender_report_user_tools payload, with the contract's limits applied.

The server enforces the same limits; applying them here keeps the payload
small and tells the user which tools were left out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .config import DEFAULT_TIMEOUT_S

MAX_TOOLS = 80
DESCRIPTION_LIMIT = 1_000
SCHEMA_LIMIT = 8 * 1024


def _timeout_int(value: Any) -> int:
    try:
        return max(1, round(float(value)))
    except (TypeError, ValueError):
        return int(DEFAULT_TIMEOUT_S)


def _schema_of(tool: Any) -> dict:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if schema is None and isinstance(tool, dict):
        schema = tool.get("input_schema") or tool.get("inputSchema")
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def _field(tool: Any, name: str) -> Any:
    return tool.get(name) if isinstance(tool, dict) else getattr(tool, name, None)


def build_report(servers: Iterable[tuple]) -> tuple[list[dict], list[dict]]:
    """(tools, dropped) from [(server_name, trusted, tools[, timeout_s])].

    `tools` are mcp Tool objects or dicts with name/description/input_schema.
    Each entry carries the server's Timeout setting as int seconds; the
    server waits that long (plus a margin) and echoes it in user_tool_call.
    """
    tools: list[dict] = []
    dropped: list[dict] = []
    for entry in servers:
        server, trusted, server_tools = entry[0], entry[1], entry[2]
        timeout_s = _timeout_int(entry[3] if len(entry) > 3 else DEFAULT_TIMEOUT_S)
        for tool in server_tools or []:
            name = str(_field(tool, "name") or "")
            if not name:
                continue
            schema = _schema_of(tool)
            try:
                size = len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                dropped.append({"server": server, "name": name, "reason": "schema isn't JSON"})
                continue
            if size > SCHEMA_LIMIT:
                dropped.append({"server": server, "name": name,
                                "reason": f"input schema is {size} bytes (limit {SCHEMA_LIMIT})"})
                continue
            if len(tools) >= MAX_TOOLS:
                dropped.append({"server": server, "name": name,
                                "reason": f"more than {MAX_TOOLS} tools"})
                continue
            description = str(_field(tool, "description") or "")[:DESCRIPTION_LIMIT]
            tools.append({
                "server": server,
                "name": name,
                "description": description,
                "input_schema": schema,
                "trusted": bool(trusted),
                "timeout_s": timeout_s,
            })
    return tools, dropped
