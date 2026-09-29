"""One undo step per dispatched command, and "undo the assistant's changes".

Every command that changes the scene runs through :func:`run_command`,
which pushes exactly one undo step named "BlenderMCP: <command> [<name>]"
and records it here. The chat UI asks :func:`summary_since` how many
steps a turn made and calls :func:`undo_since` to take them back, which
only happens when nothing else changed the scene after the assistant did.

Blender has no Python API to read its undo stack, so this module keeps
its own list of the steps it pushed and watches depsgraph_update_post
for changes it did not make. Importable without bpy (tests); every bpy
touch is a lazy import inside a function.

Verified in Blender 5.2.2 (GUI, from a bpy.app.timers callback, which
is where the drainer runs handlers):
- bpy.ops.X() from Python never pushes an undo step by default:
  WM_operator_call_py raises op_undo_depth unless undo=True is passed.
- bpy.ops.X('EXEC_DEFAULT', True) does push its own step.
- Inside an operator without the UNDO flag, even undo=True calls push
  nothing, because the outer call already raised op_undo_depth.
- An UNDO-flag operator pushes only when itself called with undo=True,
  and then under its static bl_label.
- bpy.ops.ed.undo_push(message=...) works from a timer (a window is in
  context) and names the step.
So handlers run inside a non-UNDO INTERNAL wrapper operator (swallows
nested pushes) and we push the named step ourselves afterwards.

Depsgraph updates are deferred to the next event-loop pass, so a
dispatch flushes them with view_layer.update() while still suppressed;
ed.undo fires undo_post and its depsgraph update synchronously.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

PREFIX = "BlenderMCP: "
MAX_STEPS = 256
NAME_CAP = 40
LABEL_CAP = 63  # Blender stores undo step names in char[64]
NAME_KEYS = ("name", "object", "objects", "target", "camera")

DIRTY_REASON = ("You changed the scene after the assistant did; "
                "use Ctrl+Z / Edit > Undo History instead.")

# Commands that must not run inside the wrapper operator: arbitrary code
# may load a file, which frees the running operator.
NO_WRAP = frozenset({"execute_code"})

OPERATOR_IDNAME = "blendermcp.undo_scope"


@dataclass(frozen=True)
class Step:
    """One undo step this module pushed."""

    label: str
    at: float
    command: str


_steps: deque = deque(maxlen=MAX_STEPS)
_evicted_at = 0.0     # time of the newest step dropped off the bounded list
_foreign_at = 0.0     # time of the last scene change we did not make
_suppress = 0         # >0 while our own dispatch or undo runs
_dispatch_depth = 0   # >0 while a command runs; nested commands push nothing
_scope_updates = 0    # depsgraph updates seen while suppressed
_pending: list = []   # jobs handed to the wrapper operator

clock: Callable[[], float] = time.monotonic


# --- recording (bpy-free) ------------------------------------------------------

def _short_name(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    for key in NAME_KEYS:
        val = params.get(key)
        if isinstance(val, (list, tuple)):
            if len(val) != 1:
                continue
            val = val[0]
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())
    return ""


def label_for(command: str, params: Any = None, failed: bool = False) -> str:
    """Undo step name, e.g. "BlenderMCP: add_primitive Cube"."""
    base = f"{PREFIX}{command}"
    suffix = " (failed)" if failed else ""
    name = _short_name(params)
    room = min(NAME_CAP, LABEL_CAP - len(base) - len(suffix) - 1)
    if name and room > 3:
        if len(name) > room:
            name = name[: room - 3].rstrip() + "..."
        base = f"{base} {name}"
    return (base + suffix)[:LABEL_CAP]


def record(command: str, label: str) -> Step:
    """Remember a pushed step."""
    global _evicted_at
    if len(_steps) == _steps.maxlen:
        _evicted_at = _steps[0].at
    step = Step(label=label, at=clock(), command=command)
    _steps.append(step)
    return step


def clear() -> None:
    """Forget every recorded step and any foreign change."""
    global _evicted_at, _foreign_at
    _steps.clear()
    _evicted_at = 0.0
    _foreign_at = 0.0


def _since(t: float) -> list:
    return [s for s in _steps if s.at >= t]


def steps_since(t: float) -> int:
    """How many recorded steps were pushed at or after monotonic time t."""
    return len(_since(t))


def last_steps(n: int) -> list:
    """The newest n steps, oldest first, as dicts."""
    if n <= 0:
        return []
    return [{"label": s.label, "at": s.at, "command": s.command} for s in list(_steps)[-n:]]


def is_dirty() -> bool:
    """True when something else changed the scene after our last step."""
    return bool(_steps) and _foreign_at > _steps[-1].at


@contextmanager
def suppressed():
    """Treat scene changes inside this block as ours."""
    global _suppress
    _suppress += 1
    try:
        yield
    finally:
        _suppress -= 1


def note_scene_change() -> None:
    """A depsgraph update happened; attribute it to us or to someone else."""
    global _foreign_at, _scope_updates
    if _suppress:
        _scope_updates += 1
    else:
        _foreign_at = clock()


def note_undo_redo() -> None:
    """The user undid or redid: our list no longer matches the stack."""
    if not _suppress:
        clear()


TOO_MANY_REASON = "Too many changes to undo from here; use Edit > Undo History instead."


def _blocker(t: float, steps: list) -> str:
    """Why the steps since t can't be undone as a block, or "" if they can."""
    if not steps:
        return "Nothing to undo."
    if _evicted_at and t <= _evicted_at:
        return TOO_MANY_REASON
    if _foreign_at > steps[0].at:
        return DIRTY_REASON
    limit = _undo_limit()
    if limit is not None and len(steps) > limit:
        return (f"That is {len(steps)} steps but Blender keeps only {limit} "
                "(Preferences > System > Undo Steps); use Edit > Undo History instead.")
    return ""


def summary_since(t: float) -> dict:
    """What undo_since(t) would do, for the UI."""
    steps = _since(t)
    reason = _blocker(t, steps)
    return {
        "steps": len(steps),
        "labels": [s.label for s in steps],
        "can_undo": not reason,
        "reason": reason,
    }


def undo_since(t: float) -> tuple:
    """Undo every step recorded since t, or nothing. Returns (undone, message)."""
    summary = summary_since(t)
    if not summary["can_undo"]:
        return 0, summary["reason"]
    want = summary["steps"]
    undone = 0
    with suppressed():
        try:
            for _ in range(want):
                if not _undo_once():
                    break
                _steps.pop()
                undone += 1
        finally:
            _flush_depsgraph()
    if undone < want:
        return undone, f"Undid {undone} of {want} steps; Blender refused the rest."
    return undone, f"Undid {undone} step{'s' if undone != 1 else ''}."


# --- dispatch ------------------------------------------------------------------

def _is_error_result(result: Any) -> bool:
    return isinstance(result, dict) and (
        result.get("status") == "error" or bool(result.get("error")))


def run_command(command: str, params: Any, fn: Callable[[], Any], undo: bool = True) -> Any:
    """Run a command handler as at most one named undo step.

    Read-only commands (undo=False) push nothing. A failed handler pushes
    a "(failed)" step only if it touched the scene, so Ctrl+Z can revert a
    partial change. Exceptions propagate after the step is pushed.
    """
    global _dispatch_depth
    if _dispatch_depth or not _undo_available():
        # Nested commands belong to the outer command's step.
        return fn()
    _dispatch_depth += 1
    try:
        with suppressed():
            if not undo:
                try:
                    return fn()
                finally:
                    _flush_depsgraph()
            before = _scope_updates
            job: dict = {}
            try:
                if command in NO_WRAP:
                    job["fn"] = fn
                    _execute_job(job)
                    job.pop("fn", None)
                else:
                    _run_wrapped(fn, job)
            finally:
                _flush_depsgraph()
            failed = "exc" in job or _is_error_result(job.get("result"))
            if not failed or _scope_updates != before:
                label = label_for(command, params, failed)
                if _push_undo(label):
                    record(command, label)
    finally:
        _dispatch_depth -= 1
    if "exc" in job:
        raise job["exc"]
    return job.get("result")


# --- bpy side (lazy) -----------------------------------------------------------

def _undo_available() -> bool:
    try:
        import bpy
    except ImportError:
        return False
    # Background Blender has no undo stack (workers, bpy module builds).
    return not bpy.app.background


def _undo_limit() -> int | None:
    try:
        import bpy
        return int(bpy.context.preferences.edit.undo_steps)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _window_override():
    import bpy
    if bpy.context.window is not None:
        return bpy.context.temp_override()
    wins = bpy.context.window_manager.windows
    if not wins:
        return bpy.context.temp_override()
    return bpy.context.temp_override(window=wins[0], screen=wins[0].screen)


def _run_wrapped(fn: Callable[[], Any], job: dict) -> None:
    """Run fn inside the wrapper operator so nested operators push nothing."""
    import bpy
    job["fn"] = fn
    _pending.append(job)
    try:
        op = getattr(getattr(bpy.ops, "blendermcp", None), "undo_scope", None)
        ran = False
        if op is not None:
            try:
                op()
                ran = "done" in job
            except RuntimeError as e:
                if "done" not in job:
                    print(f"[BlenderMCP] undo wrapper unavailable, running bare: {e}")
        if not ran:
            _execute_job(job)
    finally:
        _pending.remove(job)
        job.pop("fn", None)


def _execute_job(job: dict) -> None:
    job["done"] = True
    try:
        job["result"] = job["fn"]()
    except Exception as e:  # noqa: BLE001  re-raised by run_command after the push
        job["exc"] = e


def _push_undo(label: str) -> bool:
    import bpy
    try:
        with _window_override():
            return "FINISHED" in bpy.ops.ed.undo_push(message=label)
    except (RuntimeError, AttributeError) as e:
        print(f"[BlenderMCP] undo_push failed: {e}")
        return False


def _undo_once() -> bool:
    import bpy
    try:
        with _window_override():
            if not bpy.ops.ed.undo.poll():
                return False
            return "FINISHED" in bpy.ops.ed.undo()
    except (RuntimeError, AttributeError) as e:
        print(f"[BlenderMCP] undo failed: {e}")
        return False


def _flush_depsgraph() -> None:
    """Evaluate pending depsgraph updates now, while they count as ours."""
    try:
        import bpy
        seen = set()
        for win in bpy.context.window_manager.windows:
            layer = win.view_layer
            if layer is not None and layer.as_pointer() not in seen:
                seen.add(layer.as_pointer())
                layer.update()
    except (ImportError, RuntimeError, AttributeError, ReferenceError) as e:
        print(f"[BlenderMCP] depsgraph flush failed: {e}")


def _on_depsgraph_update_post(scene=None, depsgraph=None):
    note_scene_change()


def _on_undo_redo_post(*_args):
    note_undo_redo()


def _on_load_post(*_args):
    # Loading a file resets Blender's undo stack.
    clear()


_HANDLERS = (
    ("depsgraph_update_post", _on_depsgraph_update_post),
    ("undo_post", _on_undo_redo_post),
    ("redo_post", _on_undo_redo_post),
    ("load_post", _on_load_post),
)

_operator_cls = None


def _make_operator():
    import bpy

    class BLENDERMCP_OT_undo_scope(bpy.types.Operator):
        """Runs a dispatched command; no UNDO flag, so nested ops push nothing."""

        bl_idname = OPERATOR_IDNAME
        bl_label = "BlenderMCP Command"
        bl_options = {'INTERNAL'}  # noqa: RUF012  Blender reads it as a class attribute

        def execute(self, context):
            if _pending:
                _execute_job(_pending[-1])
            return {'FINISHED'}

    return BLENDERMCP_OT_undo_scope


def register() -> None:
    """Register the wrapper operator and the scene-change handlers."""
    global _operator_cls
    import bpy
    if _operator_cls is None:
        _operator_cls = _make_operator()
        bpy.utils.register_class(_operator_cls)
    for name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        bpy.app.handlers.persistent(fn)  # survive file loads
        if fn not in handlers:
            handlers.append(fn)


def unregister() -> None:
    """Remove what register() added."""
    global _operator_cls
    import bpy
    for name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        while fn in handlers:
            handlers.remove(fn)
    if _operator_cls is not None:
        try:
            bpy.utils.unregister_class(_operator_cls)
        except RuntimeError as e:
            print(f"[BlenderMCP] could not unregister undo wrapper: {e}")
        _operator_cls = None
    clear()
