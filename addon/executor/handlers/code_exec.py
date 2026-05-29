"""Arbitrary Python code execution handler.

Powerful — runs whatever the caller sends in the current Blender Python
namespace. Output is captured and returned. Errors propagate as
exceptions for the executor's outer wrapper to turn into a status="error"
response.

On failure we include the FULL Python traceback + any stdout/stderr the
script printed BEFORE the crash in the raised message. The dispatch
layer surfaces this in the MCP tool result's ``error`` field, so an
LLM-side client can self-diagnose without the user having to paste
console traces. Long captures are truncated to keep response payloads
reasonable; the exact byte counts are reported in the truncation marker
so callers know what was dropped.
"""

from __future__ import annotations

import io
import re
import traceback as _traceback
from contextlib import redirect_stderr, redirect_stdout

import bpy

from ..registry import command


# Caps tuned for typical LLM-context budgets: full traceback for any
# normal-depth crash, stdout big enough to capture a verbose-debug run,
# stderr small (uncommon path). Total worst-case payload ~13KB.
_TRACEBACK_LIMIT = 8192
_STDOUT_LIMIT = 4096
_STDERR_LIMIT = 1024


def _truncate_middle(s: str, limit: int) -> str:
    """Keep head + tail, drop the middle when too long.

    Tracebacks are most useful at the top (where the user's code is) and
    bottom (where bpy raised). The middle is usually the addon/dispatch
    plumbing the caller doesn't need to see again.
    """
    if len(s) <= limit:
        return s
    half = (limit - 50) // 2
    head = s[:half]
    tail = s[-half:]
    return f"{head}\n... [{len(s) - limit} bytes truncated from middle] ...\n{tail}"


def _strip_addon_frames(tb: str) -> str:
    """Hide the addon's executor frames from the traceback.

    The user's dispatched code runs via ``exec(code, namespace)`` inside
    this handler. The traceback shows our handler's frame above the
    user's code's frame, which is noise — the caller didn't write this
    handler. Strip the leading frames that point inside
    ``addon/executor/handlers/code_exec.py`` so what's left starts at
    the user's ``<string>`` frame.
    """
    lines = tb.splitlines(keepends=True)
    out = []
    skip = False
    for line in lines:
        # Frame headers look like:  File "path/to/code_exec.py", line 33, in execute_code
        if re.match(r'\s*File "[^"]*code_exec\.py"', line):
            skip = True
            continue
        if skip:
            # The next line is the source-line snippet for the frame we're
            # skipping; swallow it too. Then resume.
            skip = False
            continue
        out.append(line)
    return "".join(out)


class CodeExecHandlersMixin:
    """`execute_code` command."""

    @command("execute_code")
    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution.
        # Capture stdout AND stderr so we can include whatever the script
        # printed (even if it later crashed) in the result.
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        namespace = {"bpy": bpy}

        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(code, namespace)
        except SyntaxError as se:
            # SyntaxError carries lineno/offset/text — surface them cleanly
            # without dumping the full traceback (which is just our exec
            # frame plus the SyntaxError summary).
            parts = [
                f"Code execution error (SyntaxError) at line {se.lineno}: {se.msg}"
            ]
            if se.text:
                parts.append(f"  {se.text.rstrip()}")
                if se.offset:
                    parts.append("  " + " " * (se.offset - 1) + "^")
            stdout_before = stdout_buf.getvalue()
            if stdout_before:
                parts.append(
                    f"\n[stdout before crash, {len(stdout_before)} bytes]:\n"
                    f"{_truncate_middle(stdout_before, _STDOUT_LIMIT)}"
                )
            raise Exception("\n".join(parts))
        except Exception as e:
            # Full traceback (formatted) + any captured output. Strip our
            # own handler frame so the trace starts at the user's code.
            tb_str = _strip_addon_frames(_traceback.format_exc())
            stdout_before = stdout_buf.getvalue()
            stderr_before = stderr_buf.getvalue()

            parts = [f"Code execution error ({type(e).__name__}): {e}"]
            parts.append(
                f"\n[traceback, {len(tb_str)} bytes]:\n"
                f"{_truncate_middle(tb_str, _TRACEBACK_LIMIT)}"
            )
            if stdout_before:
                parts.append(
                    f"\n[stdout before crash, {len(stdout_before)} bytes]:\n"
                    f"{_truncate_middle(stdout_before, _STDOUT_LIMIT)}"
                )
            if stderr_before:
                parts.append(
                    f"\n[stderr before crash, {len(stderr_before)} bytes]:\n"
                    f"{_truncate_middle(stderr_before, _STDERR_LIMIT)}"
                )
            raise Exception("\n".join(parts))

        # Success path. Include stderr if anything was written there
        # (deprecation warnings, bpy.ops complaints, etc.) so the caller
        # sees that too without needing a separate API.
        captured_output = stdout_buf.getvalue()
        stderr_output = stderr_buf.getvalue()
        result = {"executed": True, "result": captured_output}
        if stderr_output:
            result["stderr"] = _truncate_middle(stderr_output, _STDERR_LIMIT)
        return result
