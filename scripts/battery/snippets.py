"""Python snippets the runner executes inside the GUI Blender.

They reach the add-on's chat modules by module-name suffix, the same way the
Chat tab's own code is wired, so the battery drives exactly what a user
clicks. Every snippet prints ``MARK + json`` on its last line (see
``run_canary.run_code``). Values are embedded with ``repr(json)`` so no
prompt text or expression can break out of the string it lives in.
"""

from __future__ import annotations

import json

MARK = "CANARY_JSON:"

PRELUDE = f"""
import sys, json, bpy
_MARK = {MARK!r}
def _mod(suffix):
    for k, m in list(sys.modules.items()):
        if k.endswith(suffix):
            return m
    raise RuntimeError("add-on module not loaded: " + suffix)
def _out(v):
    print(_MARK + json.dumps(v, default=str))
"""

CHAT = PRELUDE + """
_client = _mod(".chat.client")
_st = _mod(".chat.state").chat_state
"""


def _lit(value) -> str:
    """A Python expression that evaluates to ``value`` (JSON round trip)."""
    return f"json.loads({json.dumps(json.dumps(value))})"


# Scene snapshot. Objects: transforms, world AABB, materials; annotation
# layers with stroke counts and point bounds; then any python: checks.
SNAPSHOT_BODY = """
from mathutils import Vector
_scene = bpy.context.scene
def _r(v, n=4):
    return [round(float(x), n) for x in v]
def _aabb(o):
    if o.type in ("MESH", "CURVE", "FONT", "SURFACE", "META") and hasattr(o, "bound_box"):
        pts = [o.matrix_world @ Vector(c) for c in o.bound_box]
        return _r([min(p[i] for p in pts) for i in range(3)]), _r([max(p[i] for p in pts) for i in range(3)])
    t = _r(o.matrix_world.translation)
    return t, t
def _mat_info(m):
    info = {"color": _r(m.diffuse_color), "metallic": round(float(m.metallic), 3),
            "roughness": round(float(m.roughness), 3), "source": "viewport"}
    if m.use_nodes and m.node_tree:
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                bc = n.inputs.get("Base Color")
                if bc is not None:
                    info["color"] = None if bc.is_linked else _r(bc.default_value)
                for key, name in (("metallic", "Metallic"), ("roughness", "Roughness")):
                    s = n.inputs.get(name)
                    if s is not None and not s.is_linked:
                        info[key] = round(float(s.default_value), 3)
                info["source"] = "principled"
                break
    return info
_objs, _mats = [], {}
for o in _scene.objects:
    lo, hi = _aabb(o)
    names = []
    for slot in getattr(o, "material_slots", []):
        if slot.material:
            names.append(slot.material.name)
            if slot.material.name not in _mats:
                _mats[slot.material.name] = _mat_info(slot.material)
    _objs.append({"name": o.name, "type": o.type, "parent": o.parent.name if o.parent else None,
                  "loc": _r(o.matrix_world.translation), "rot": _r(o.rotation_euler),
                  "scale": _r(o.scale), "dims": _r(o.dimensions), "bmin": lo, "bmax": hi,
                  "mats": names, "visible": bool(o.visible_get())})
_ann = {}
_a = getattr(_scene, "annotation", None)
if _a is not None:
    for layer in _a.layers:
        n, pts = 0, []
        for fr in layer.frames:
            for s in fr.strokes:
                n += 1
                pts.extend(tuple(p.co) for p in s.points)
        entry = {"strokes": n}
        if pts:
            entry["bmin"] = _r([min(p[i] for p in pts) for i in range(3)])
            entry["bmax"] = _r([max(p[i] for p in pts) for i in range(3)])
        _ann[layer.info] = entry
_py = {}
_ns = {"bpy": bpy, "D": bpy.data, "C": bpy.context, "scene": _scene, "Vector": Vector,
       "objects": bpy.data.objects, "obj": bpy.data.objects.get}
for key, expr in _EXPRS.items():
    try:
        _py[key] = {"value": eval(expr, dict(_ns))}
    except Exception as e:
        _py[key] = {"error": type(e).__name__ + ": " + str(e)[:200]}
_out({"file": bpy.data.filepath, "scene": _scene.name, "objects": _objs, "materials": _mats,
      "annotations": _ann, "python": _py})
"""


def snapshot(exprs: dict[str, str] | None = None) -> str:
    return PRELUDE + f"_EXPRS = {_lit(exprs or {})}\n" + SNAPSHOT_BODY


def remove_default_cube() -> str:
    return PRELUDE + """
o = bpy.data.objects.get("Cube")
removed = False
if o is not None and o.type == "MESH":
    bpy.data.objects.remove(o, do_unlink=True)
    removed = True
_out({"removed": removed, "objects": sorted(x.name for x in bpy.context.scene.objects)})
"""


def set_scene(name: str) -> str:
    return PRELUDE + f"""
_name = {_lit(name)}
sc = bpy.data.scenes.get(_name)
if sc is None:
    _out({{"ok": False, "error": "no scene " + _name}})
else:
    for w in bpy.context.window_manager.windows:
        w.scene = sc
    _out({{"ok": True, "scene": bpy.context.window_manager.windows[0].scene.name}})
"""


def run_setup_python(code: str) -> str:
    # exec() keeps the user's snippet from seeing or clobbering our helpers.
    return PRELUDE + f"""
_code = {_lit(code)}
_g = {{"bpy": bpy, "__name__": "battery_setup"}}
exec(compile(_code, "battery_setup", "exec"), _g)
_out({{"ok": True, "objects": sorted(x.name for x in bpy.context.scene.objects)}})
"""


def poll(since_turn: int) -> str:
    return CHAT + f"""
_since = {int(since_turn)}
with _st.lock:
    pend = _st.pending_approval
    msgs = []
    for m in _st.messages:
        if m.get("turn", 0) > _since:
            msgs.append({{k: (v[:4000] if isinstance(v, str) else v) for k, v in m.items()
                         if k in ("role", "text", "name", "ok", "ms", "turn")}})
    _out({{"busy": _st.busy, "status": _st.status, "turn": _st.turn,
          "approval": pend.get("prompt") if pend else None, "last_error": _st.last_error,
          "available": _st.available, "backend_used": _st.backend_used,
          "state_id": id(_st), "messages": msgs}})
"""


def send(text: str) -> str:
    return CHAT + f"""
_before = _st.turn
ok, problem = _client.send({_lit(text)})
_out({{"ok": bool(ok), "problem": problem, "turn_before": _before, "turn": _st.turn}})
"""


def resolve(allowed: bool) -> str:
    return PRELUDE + f"""
_out({{"resolved": bool(_mod(".chat.elicitation").resolve_approval({bool(allowed)!r}))}})
"""


STOP = CHAT + """
_out({"stopped": bool(_client.stop()), "busy": _st.busy})
"""

CLEAR = CHAT + """
if _st.busy:
    _client.stop()
_st.clear()
_out({"busy": _st.busy, "turn": _st.turn, "available": _st.available, "state_id": id(_st)})
"""

BACKEND_REFRESH = CHAT + """
_st.backend = None
_st.backend_error = None
_out({"problem": _client.refresh_backend()})
"""

BACKEND_STATE = CHAT + """
with _st.lock:
    _out({"backend": _st.backend, "error": _st.backend_error, "busy": _st.busy})
"""


def set_backend(provider: str, model: str = "", base_url: str = "",
                key_path: str | None = None) -> str:
    """Switch backend through the add-on (blender_set_chat_backend is add-on only).

    A key never appears in the snippet: it is read from ``key_path`` (a file
    the runner wrote into the bind-mounted projects directory) and the file
    is deleted before anything else happens.
    """
    key_code = "_key = ''\n"
    if key_path:
        key_code = f"""
import os
_kp = {_lit(key_path)}
try:
    with open(_kp) as f:
        _key = f.read().strip()
finally:
    try:
        os.remove(_kp)
    except OSError:
        pass
"""
    return CHAT + key_code + f"""
_st.backend = None
_st.backend_error = None
_problem = _client.set_backend({_lit(provider)}, {_lit(model)}, {_lit(base_url)}, _key)
del _key
_out({{"problem": _problem}})
"""
