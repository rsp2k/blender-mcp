"""Opt-in QA logging of tool calls. OFF unless QA_LOG is set.

The default production logs carry no payloads at all: uvicorn emits
``POST / 200 OK`` (the path is always ``/``, since MCP is a single
JSON-RPC endpoint) and the SDK emits ``Processing request of type
CallToolRequest``. Neither says which tool ran, for whom, with what
arguments, or what came back. Good for privacy and log volume, useless
for QA -- the dispatch-timeout hunt on 2026-05-30 had nothing to
correlate a request to a target client, which is why the timeout
*response body* had to be enriched instead.

This middleware fills that gap on demand. One JSON object per line so
the output is jq-able:

    docker compose logs blender-mcp | grep '"evt":"tool"' | jq -r '[.tool,.ms]|@tsv'

Three levels via a single env var:

    QA_LOG=off     (or unset) nothing is logged. THE DEFAULT.
    QA_LOG=meta    tool name, user, client, duration, payload SIZES.
                   No argument or result content leaves the process.
    QA_LOG=full    adds truncated + redacted arguments and results.

``meta`` exists because the common QA question ("what is generating
300 tool calls in 40 minutes?") needs names and counts, not payloads,
and payloads are the entire privacy exposure. Reach for ``full`` only
when you actually need to see content.

WHAT ``full`` WILL WRITE TO DISK: this server's tool surface includes
``blender_execute_code``, so ``full`` logs arbitrary Python bodies and
whatever scene data comes back. Treat the output as sensitive, keep it
off in any shared deployment, and prefer ``meta`` unless you are
actively debugging a payload.

Other knobs:

    QA_LOG_PATH       file to append to. Default: stderr (so it lands
                      in ``docker logs`` with everything else).
    QA_LOG_MAX_CHARS  per-field truncation for ``full``. Default 2000.
    QA_LOG_TOOLS      comma-separated allowlist of tool names. Default
                      all. Useful to watch one noisy tool without
                      drowning in the rest.

Deliberately NOT implemented: rotation. If you point QA_LOG_PATH at a
file you own the cleanup. The expected mode is short bursts against
stderr, not a permanent appender.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Argument keys whose values are replaced wholesale. Substring match on
# the lower-cased key, so "api_key", "X-Auth-Token" and "refresh_token"
# are all caught. Cheap insurance: none of the current 42 tools take a
# credential, but tools get added and this is the kind of thing nobody
# remembers to revisit.
_SECRET_HINTS = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "jwt",
)

_LEVELS = ("off", "meta", "full")


def _level() -> str:
    raw = os.environ.get("QA_LOG", "off").strip().lower()
    # Accept the obvious truthy spellings as "full" so QA_LOG=1 does
    # something useful instead of silently staying off.
    if raw in ("1", "true", "yes", "on"):
        return "full"
    return raw if raw in _LEVELS else "off"


def _max_chars() -> int:
    try:
        return max(0, int(os.environ.get("QA_LOG_MAX_CHARS", "2000")))
    except ValueError:
        return 2000


def _tool_allowlist() -> Optional[frozenset[str]]:
    raw = os.environ.get("QA_LOG_TOOLS", "").strip()
    if not raw:
        return None
    names = {n.strip() for n in raw.split(",") if n.strip()}
    return frozenset(names) or None


def _redact(value: Any) -> Any:
    """Recursively blank out values whose key looks like a credential."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(h in str(k).lower() for h in _SECRET_HINTS):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    # Keep the tail length so a reader can tell how much was dropped;
    # a bare "..." hides whether it lost 10 chars or 10 MB.
    return f"{text[:limit]}...<+{len(text) - limit} chars>"


def _stringify(value: Any) -> str:
    """Best-effort compact text for arbitrary tool payloads."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return repr(value)


def _result_text(result: Any) -> str:
    """Pull readable text out of a ToolResult without assuming its shape.

    ToolResult's internals move between FastMCP versions, so this reads
    defensively rather than binding to a specific attribute layout.
    """
    content = getattr(result, "content", None)
    if content is None:
        return _stringify(result)
    parts = []
    for block in content if isinstance(content, (list, tuple)) else [content]:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else _stringify(block))
    return "\n".join(parts)


class QALoggingMiddleware(Middleware):
    """Emit one JSONL record per tool call when QA_LOG is enabled.

    Reads its configuration once at construction. Flipping QA_LOG means
    restarting the server, which is intentional: re-reading the
    environment per call would put an os.environ lookup on every
    dispatch for a setting that changes approximately never.

    Every failure inside this middleware is swallowed. A logging bug
    must never be able to break a tool call.
    """

    def __init__(self) -> None:
        self.level = _level()
        self.max_chars = _max_chars()
        self.allow = _tool_allowlist()
        self._stream = None
        if self.level != "off":
            self._stream = self._open_stream()
            # Announce loudly. "full" silently recording every script
            # body is exactly the sort of thing that should never be a
            # surprise to whoever reads the logs later.
            where = os.environ.get("QA_LOG_PATH") or "stderr"
            print(
                f"[QA_LOG] tool-call logging ENABLED level={self.level} -> {where}"
                + (
                    "  (records arbitrary code + scene payloads)"
                    if self.level == "full"
                    else ""
                ),
                file=sys.stderr,
                flush=True,
            )

    @property
    def enabled(self) -> bool:
        return self.level != "off"

    @staticmethod
    def _open_stream():
        path = os.environ.get("QA_LOG_PATH", "").strip()
        if not path:
            return sys.stderr
        try:
            # Line-buffered append so records land even if the process
            # is killed without a clean shutdown.
            return open(path, "a", buffering=1, encoding="utf-8")
        except Exception as exc:
            print(
                f"[QA_LOG] cannot open {path!r} ({exc}); falling back to stderr",
                file=sys.stderr,
                flush=True,
            )
            return sys.stderr

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        if not self.enabled:
            return await call_next(context)

        tool = getattr(context.message, "name", None) or "<unknown>"
        if self.allow is not None and tool not in self.allow:
            return await call_next(context)

        args = getattr(context.message, "arguments", None) or {}
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._emit(tool, args, None, exc, time.perf_counter() - started)
            raise
        self._emit(tool, args, result, None, time.perf_counter() - started)
        return result

    def _emit(
        self,
        tool: str,
        args: Any,
        result: Any,
        exc: Optional[BaseException],
        elapsed: float,
    ) -> None:
        try:
            rec: dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "evt": "tool",
                "tool": tool,
                "ms": round(elapsed * 1000, 1),
                "ok": exc is None,
            }
            uid, cid = self._identity()
            if uid:
                # Already a SHA256 of the upstream user id, not an email.
                rec["user"] = uid
            if cid:
                rec["client_id"] = cid

            args_text = _stringify(_redact(args))
            rec["args_len"] = len(args_text)
            result_text = "" if result is None else _result_text(result)
            rec["result_len"] = len(result_text)

            if exc is not None:
                rec["error"] = _clip(f"{type(exc).__name__}: {exc}", self.max_chars)

            if self.level == "full":
                rec["args"] = _clip(args_text, self.max_chars)
                if result is not None:
                    rec["result"] = _clip(result_text, self.max_chars)

            print(json.dumps(rec, ensure_ascii=False), file=self._stream, flush=True)
        except Exception:
            # Never let QA instrumentation take down a tool call.
            pass

    @staticmethod
    def _identity() -> tuple[Optional[str], Optional[str]]:
        """Resolve (user_id, downstream_client_id), both best-effort.

        Deferred imports keep this module cheap at app-build time and
        avoid dragging bus/auth state into middleware registration,
        matching BusActivityMiddleware.
        """
        uid = None
        cid = None
        try:
            from .client_role import current_downstream_client_id

            cid = current_downstream_client_id.get()
        except Exception:
            pass
        try:
            from .bus_tools import _resolve_user_id

            uid = _resolve_user_id(None)
        except Exception:
            pass
        return uid, cid
