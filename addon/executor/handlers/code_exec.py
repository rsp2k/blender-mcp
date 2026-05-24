"""Arbitrary Python code execution handler.

Powerful — runs whatever the caller sends in the current Blender Python
namespace. Output is captured and returned. Errors propagate as
exceptions for the executor's outer wrapper to turn into a status="error"
response.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import bpy


class CodeExecHandlersMixin:
    """`execute_code` command."""

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        # This is powerful but potentially dangerous - use with caution
        try:
            # Create a local namespace for execution
            namespace = {"bpy": bpy}

            # Capture stdout during execution, and return it as result
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)

            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")
