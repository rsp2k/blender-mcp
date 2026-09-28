"""Declarative checks: normalising a case's check list and judging it.

Judging is pure. The runner hands over three things gathered elsewhere:

- ``before`` / ``after``: scene snapshots taken in Blender by
  ``snippets.SNAPSHOT`` (objects with transforms, world bounds and
  materials; annotation layers; results of ``python:`` checks, keyed by the
  check's index);
- ``turn``: what the chat did (reply text, tool calls, approvals, errors).

Every check returns ``{"check", "ok", "observed", "expect"}`` so a failure in
the report says what was actually there.
"""

from __future__ import annotations

import colorsys
import math
from typing import Any

# type -> parameter used when the check is given a scalar instead of a mapping
CHECK_TYPES: dict[str, str | None] = {
    "object_exists": "name",
    "object_absent": "name",
    "count_type": "type",
    "dims_approx": None,
    "location_approx": None,
    "rests_on": None,
    "has_material": None,
    "annotation_strokes_min": "min",
    "annotation_near": "name",
    "reply_contains": "text",
    "reply_not_contains": "text",
    "max_tool_calls": "max",
    "tool_used": "tool",
    "tool_not_used": "tool",
    "no_errors": "enabled",
    "scene_unchanged": "enabled",
    "python": "expr",
}

SELECTOR_KEYS = ("name", "name_contains", "type", "new", "pick")
PICKS = ("first", "largest", "highest", "lowest", "tallest")

_REQUIRED = {
    "dims_approx": ("dims",),
    "location_approx": ("location",),
    "rests_on": ("base",),
    "count_type": ("type",),
    "reply_contains": ("text",),
    "reply_not_contains": ("text",),
    "max_tool_calls": ("max",),
    "tool_used": ("tool",),
    "tool_not_used": ("tool",),
    "annotation_strokes_min": ("min",),
    "python": ("expr",),
}
_SELECTOR_CHECKS = ("object_exists", "object_absent", "dims_approx", "location_approx",
                    "rests_on", "has_material", "annotation_near")


def normalise_check(raw: Any) -> dict:
    """``{kind: params}`` or ``{kind: scalar}`` -> ``{"check": kind, **params}``."""
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError("a check is a one-key mapping like {object_exists: {name: Cube}}")
    (ctype, params), = raw.items()
    if ctype not in CHECK_TYPES:
        raise ValueError(f"unknown check type {ctype!r}")
    if not isinstance(params, dict):
        scalar_key = CHECK_TYPES[ctype]
        if scalar_key is None:
            raise ValueError(f"{ctype} needs a mapping of parameters")
        params = {scalar_key: params}
    if "check" in params:
        raise ValueError("'check' is a reserved parameter name")
    if True in params:
        # YAML 1.1 reads a bare `on:` key as the boolean True.
        raise ValueError("a key parsed as true (YAML reads 'on'/'yes' that way); "
                         "rests_on takes 'base'")
    out = {"check": ctype, **params}
    for key in _REQUIRED.get(ctype, ()):
        if key not in out:
            raise ValueError(f"{ctype} needs '{key}'")
    if ctype in _SELECTOR_CHECKS and not any(k in out for k in ("name", "name_contains", "type", "new")):
        raise ValueError(f"{ctype} needs a selector (name, name_contains, type or new)")
    if out.get("pick", "first") not in PICKS:
        raise ValueError(f"pick must be one of {PICKS}")
    for key in ("dims", "location"):
        if key in out:
            v = out[key]
            if not (isinstance(v, list) and len(v) == 3
                    and all(x is None or isinstance(x, (int, float)) for x in v)):
                raise ValueError(f"{key} must be a list of three numbers (null to skip an axis)")
    if ctype == "has_material" and out.get("color") is not None:
        target_color(out["color"])  # raises on an unknown name
    if ctype in ("reply_contains", "reply_not_contains"):
        t = out["text"]
        if not (isinstance(t, str) or (isinstance(t, list) and t and all(isinstance(x, str) for x in t))):
            raise ValueError("text must be a string or a list of strings")
    if ctype == "location_approx" and out.get("point", "origin") not in ("origin", "center", "bottom", "top"):
        raise ValueError("point must be origin, center, bottom or top")
    return out


# ----------------------------------------------------------------- objects

def _objects(snap: dict | None) -> list[dict]:
    return list((snap or {}).get("objects") or [])


def select(spec: dict, after: dict, before: dict | None = None) -> list[dict]:
    """Objects in ``after`` matching the selector keys of ``spec``."""
    objs = _objects(after)
    if "name" in spec:
        want = str(spec["name"])
        exact = [o for o in objs if o["name"] == want]
        objs = exact or [o for o in objs if o["name"].lower() == want.lower()]
    if "name_contains" in spec:
        needles = spec["name_contains"]
        needles = [needles] if isinstance(needles, str) else list(needles)
        objs = [o for o in objs if any(n.lower() in o["name"].lower() for n in needles)]
    if spec.get("type"):
        objs = [o for o in objs if o.get("type") == str(spec["type"]).upper()]
    if spec.get("new"):
        old = {o["name"] for o in _objects(before)}
        objs = [o for o in objs if o["name"] not in old]
    return _pick(objs, spec.get("pick", "first"))


def _size(o: dict) -> list[float]:
    return [hi - lo for lo, hi in zip(o["bmin"], o["bmax"], strict=True)]


def _pick(objs: list[dict], pick: str) -> list[dict]:
    if not objs or pick == "first":
        return objs
    key = {
        "largest": lambda o: math.prod(max(s, 1e-9) for s in _size(o)),
        "highest": lambda o: o["bmax"][2],
        "lowest": lambda o: -o["bmin"][2],
        "tallest": lambda o: _size(o)[2],
    }[pick]
    return [max(objs, key=key)]


def _point(o: dict, point: str) -> list[float]:
    center = [(lo + hi) / 2 for lo, hi in zip(o["bmin"], o["bmax"], strict=True)]
    if point == "origin":
        return list(o["loc"])
    if point == "bottom":
        return [center[0], center[1], o["bmin"][2]]
    if point == "top":
        return [center[0], center[1], o["bmax"][2]]
    return center


def _close(values: list[float], target: list, tol: float) -> bool:
    return all(t is None or abs(v - t) <= tol for v, t in zip(values, target, strict=True))


def _r(v: Any, nd: int = 3) -> Any:
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, list):
        return [_r(x, nd) for x in v]
    return v


# ----------------------------------------------------------------- colours

NAMED_COLORS = {
    # hue ranges in degrees on the sRGB-encoded colour
    "red": (345, 15), "orange": (15, 45), "yellow": (45, 70), "green": (70, 170),
    "cyan": (170, 200), "blue": (200, 255), "purple": (255, 300), "pink": (300, 345),
    "magenta": (285, 330),
}
NEUTRALS = ("white", "black", "grey", "gray", "brown")


def target_color(color: Any) -> Any:
    """A colour name, a list of names (any of them), or linear [r, g, b]."""
    if isinstance(color, list) and color and all(isinstance(c, str) for c in color):
        return [target_color(c) for c in color]
    if isinstance(color, str):
        c = color.lower()
        if c not in NAMED_COLORS and c not in NEUTRALS:
            raise ValueError(f"unknown colour name {color!r}")
        return c
    if isinstance(color, list) and len(color) in (3, 4) and all(isinstance(x, (int, float)) for x in color):
        return [float(x) for x in color[:3]]
    raise ValueError("color must be a name or [r, g, b]")


def linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def color_matches(linear_rgb: list[float] | None, color: Any, tol: float = 0.15) -> bool:
    """Does a Blender (linear) base colour read as ``color``?

    Names are judged on hue/saturation/value of the sRGB-encoded colour, the
    way a person would name it; an ``[r, g, b]`` target (linear, like
    Blender's field) is compared per channel within ``tol``.
    """
    if not linear_rgb:
        return False
    target = target_color(color)
    rgb = [float(x) for x in linear_rgb[:3]]
    if isinstance(target, list) and target and isinstance(target[0], str):
        return any(color_matches(linear_rgb, name, tol) for name in target)
    if isinstance(target, list):
        return all(abs(a - b) <= tol for a, b in zip(rgb, target, strict=True))
    h, s, v = colorsys.rgb_to_hsv(*(linear_to_srgb(x) for x in rgb))
    deg = h * 360
    if target == "white":
        return v >= 0.8 and s <= 0.2
    if target == "black":
        return v <= 0.2
    if target in ("grey", "gray"):
        return s <= 0.2 and 0.15 <= v <= 0.85
    if target == "brown":
        return 10 <= deg <= 50 and s >= 0.3 and 0.15 <= v <= 0.7
    if s < 0.3 or v < 0.2:
        return False
    lo, hi = NAMED_COLORS[target]
    return (deg >= lo or deg < hi) if lo > hi else lo <= deg < hi


# ----------------------------------------------------------------- judging

def _result(check: dict, ok: bool, observed: Any, expect: Any = None) -> dict:
    return {"check": check["check"], "ok": bool(ok), "observed": observed,
            "expect": expect if expect is not None else _describe(check)}


def _describe(check: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in check.items() if k != "check")


def _one(check: dict, after: dict, before: dict | None) -> tuple[dict | None, list[dict]]:
    found = select(check, after, before)
    return (found[0] if found else None), found


def _names(objs: list[dict], limit: int = 8) -> list[str]:
    names = [o["name"] for o in objs[:limit]]
    return names + ([f"... {len(objs) - limit} more"] if len(objs) > limit else [])


def judge(check: dict, after: dict, before: dict | None, turn: dict, index: int = 0) -> dict:
    """One check -> {"check", "ok", "observed", "expect"}. Never raises."""
    try:
        return _JUDGES[check["check"]](check, after or {}, before, turn or {}, index)
    except Exception as e:  # noqa: BLE001 - a broken check is a failed check, not a crash
        return _result(check, False, f"check error: {type(e).__name__}: {e}")


def judge_all(checks: list[dict], after: dict, before: dict | None, turn: dict) -> list[dict]:
    return [judge(c, after, before, turn, i) for i, c in enumerate(checks)]


def _j_exists(check, after, before, turn, i):
    _, found = _one(check, after, before)
    n = check.get("count")
    ok = len(found) == n if n is not None else bool(found)
    return _result(check, ok, _names(found) or "no match")


def _j_absent(check, after, before, turn, i):
    _, found = _one(check, after, before)
    return _result(check, not found, _names(found) or "absent")


def _j_count(check, after, before, turn, i):
    objs = _objects(after)
    kind = str(check["type"]).upper()
    objs = [o for o in objs if o.get("type") == kind]
    if check.get("new"):
        old = {o["name"] for o in _objects(before)}
        objs = [o for o in objs if o["name"] not in old]
    n = len(objs)
    ok = True
    if "eq" in check:
        ok &= n == check["eq"]
    if "min" in check:
        ok &= n >= check["min"]
    if "max" in check:
        ok &= n <= check["max"]
    return _result(check, ok, n)


def _j_dims(check, after, before, turn, i):
    obj, _ = _one(check, after, before)
    if obj is None:
        return _result(check, False, "no match")
    tol = float(check.get("tol", 0.05))
    dims = [float(x) for x in obj["dims"]]
    want = list(check["dims"])
    if check.get("any_order"):
        got_s = sorted(dims)
        want_s = sorted(x for x in want if x is not None)
        ok = len(want_s) == 3 and all(abs(a - b) <= tol for a, b in zip(got_s, want_s, strict=True))
    else:
        ok = _close(dims, want, tol)
    return _result(check, ok, {"object": obj["name"], "dims": _r(dims)})


def _j_location(check, after, before, turn, i):
    obj, _ = _one(check, after, before)
    if obj is None:
        return _result(check, False, "no match")
    point = check.get("point", "origin")
    p = _point(obj, point)
    ok = _close(p, list(check["location"]), float(check.get("tol", 0.1)))
    return _result(check, ok, {"object": obj["name"], point: _r(p)})


def _j_rests_on(check, after, before, turn, i):
    obj, _ = _one(check, after, before)
    spec = check["base"] if isinstance(check["base"], dict) else {"name": check["base"]}
    base, _ = _one(spec, after, before)
    if obj is None or base is None:
        return _result(check, False, {"object": obj and obj["name"], "base": base and base["name"]})
    tol = float(check.get("tol", 0.05))
    gap = obj["bmin"][2] - base["bmax"][2]
    cx = (obj["bmin"][0] + obj["bmax"][0]) / 2
    cy = (obj["bmin"][1] + obj["bmax"][1]) / 2
    over = base["bmin"][0] - tol <= cx <= base["bmax"][0] + tol and \
        base["bmin"][1] - tol <= cy <= base["bmax"][1] + tol
    ok = abs(gap) <= tol and over
    return _result(check, ok, {"object": obj["name"], "base": base["name"], "gap": _r(gap),
                               "center_over_base": over})


def _material_ok(m: dict, check: dict) -> bool:
    if "material" in check and str(check["material"]).lower() not in m.get("name", "").lower():
        return False
    if check.get("color") is not None and not color_matches(
            m.get("color"), check["color"], float(check.get("color_tol", 0.15))):
        return False
    for key, attr, cmp in (("metallic_min", "metallic", 1), ("metallic_max", "metallic", -1),
                           ("roughness_min", "roughness", 1), ("roughness_max", "roughness", -1)):
        if key in check:
            val = m.get(attr)
            if val is None:
                return False
            if cmp == 1 and val < check[key] or cmp == -1 and val > check[key]:
                return False
    return True


def _j_material(check, after, before, turn, i):
    _, found = _one(check, after, before)
    if not found:
        return _result(check, False, "no match")
    table = after.get("materials") or {}
    targets = found if check.get("all") else found[:1]
    observed, ok_all = [], True
    for obj in targets:
        mats = [{"name": n, **(table.get(n) or {})} for n in obj.get("mats") or []]
        ok = any(_material_ok(m, check) for m in mats)
        ok_all &= ok
        observed.append({"object": obj["name"], "materials": [
            {k: _r(v) for k, v in m.items() if k in ("name", "color", "metallic", "roughness")}
            for m in mats]})
    return _result(check, ok_all, observed if len(observed) > 1 else observed[0])


def _layers(after: dict, layer: str | None) -> dict:
    layers = after.get("annotations") or {}
    if layer:
        return {k: v for k, v in layers.items() if k == layer}
    return layers


def _j_ann_min(check, after, before, turn, i):
    layers = _layers(after, check.get("layer"))
    n = sum(int(v.get("strokes", 0)) for v in layers.values())
    return _result(check, n >= int(check["min"]), {k: v.get("strokes") for k, v in layers.items()} or 0)


def _j_ann_near(check, after, before, turn, i):
    obj, _ = _one(check, after, before)
    if obj is None:
        return _result(check, False, "no match")
    layers = [v for v in _layers(after, check.get("layer")).values() if v.get("strokes") and v.get("bmin")]
    if not layers:
        return _result(check, False, {"object": obj["name"], "strokes": 0})
    lo = [min(v["bmin"][k] for v in layers) for k in range(3)]
    hi = [max(v["bmax"][k] for v in layers) for k in range(3)]
    size = max(_size(obj) + [1e-6])
    margin = float(check.get("margin", 0.25)) * size
    center = _point(obj, "center")
    contains = all(lo[k] - margin <= center[k] <= hi[k] + margin for k in range(3))
    # Strokes that cover the whole scene would "contain" every object.
    stroke_size = max(h - l for l, h in zip(lo, hi, strict=True))
    tight = stroke_size <= float(check.get("max_ratio", 4.0)) * size + margin
    return _result(check, contains and tight, {"object": obj["name"], "object_center": _r(center),
                                               "strokes_bmin": _r(lo), "strokes_bmax": _r(hi)})


def _texts(check) -> list[str]:
    t = check["text"]
    return [t] if isinstance(t, str) else list(t)


_NOISE = str.maketrans("", "", "\"'`*“”‘’")


def _norm(text: str) -> str:
    """Lowercase, without quotes, backticks or markdown emphasis, so
    ``"Sphere_A" and **Sphere_B**`` matches ``sphere_a and sphere_b``."""
    return " ".join(text.lower().translate(_NOISE).split())


def _j_reply_contains(check, after, before, turn, i):
    reply = (turn.get("reply") or "")
    low = _norm(reply)
    texts = _texts(check)
    hits = [t for t in texts if _norm(t) in low]
    # A list means "any of these" unless all: true.
    ok = len(hits) == len(texts) if check.get("all") else bool(hits)
    return _result(check, ok, reply[:240] or "(empty reply)")


def _j_reply_not_contains(check, after, before, turn, i):
    reply = (turn.get("reply") or "")
    hits = [t for t in _texts(check) if _norm(t) in _norm(reply)]
    return _result(check, not hits, {"found": hits} if hits else "none found")


def _tool_names(turn: dict) -> list[str]:
    return [t.get("name") for t in turn.get("tools") or []]


def _j_max_tools(check, after, before, turn, i):
    n = len(turn.get("tools") or [])
    return _result(check, n <= int(check["max"]), n)


def _j_tool_used(check, after, before, turn, i):
    names = _tool_names(turn)
    return _result(check, check["tool"] in names, names)


def _j_tool_not_used(check, after, before, turn, i):
    names = _tool_names(turn)
    return _result(check, check["tool"] not in names, names)


def _j_no_errors(check, after, before, turn, i):
    if check.get("enabled") is False:
        return _result(check, True, "disabled")
    errors = list(turn.get("errors") or [])
    if check.get("tools_ok"):
        errors += [f"tool {t.get('name')} failed" for t in turn.get("tools") or [] if t.get("ok") is False]
    return _result(check, not errors, errors or "none")


def fingerprint(o: dict) -> tuple:
    return (o.get("type"), tuple(_r(list(o.get("loc") or []), 3)), tuple(_r(list(o.get("dims") or []), 3)),
            tuple(o.get("mats") or []))


def _j_unchanged(check, after, before, turn, i):
    if check.get("enabled") is False:
        return _result(check, True, "disabled")
    if before is None:
        return _result(check, False, "no before snapshot")
    b = {o["name"]: fingerprint(o) for o in _objects(before)}
    a = {o["name"]: fingerprint(o) for o in _objects(after)}
    added = sorted(set(a) - set(b))
    removed = sorted(set(b) - set(a))
    changed = sorted(n for n in set(a) & set(b) if a[n] != b[n])
    ok = not (added or removed or changed)
    return _result(check, ok, "unchanged" if ok else
                   {"added": added[:8], "removed": removed[:8], "changed": changed[:8]})


def _j_python(check, after, before, turn, i):
    res = (after.get("python") or {}).get(str(i))
    if res is None:
        return _result(check, False, "not evaluated")
    if "error" in res:
        return _result(check, False, f"error: {res['error']}")
    return _result(check, bool(res.get("value")), res.get("value"))


_JUDGES = {
    "object_exists": _j_exists, "object_absent": _j_absent, "count_type": _j_count,
    "dims_approx": _j_dims, "location_approx": _j_location, "rests_on": _j_rests_on,
    "has_material": _j_material, "annotation_strokes_min": _j_ann_min,
    "annotation_near": _j_ann_near, "reply_contains": _j_reply_contains,
    "reply_not_contains": _j_reply_not_contains, "max_tool_calls": _j_max_tools,
    "tool_used": _j_tool_used, "tool_not_used": _j_tool_not_used, "no_errors": _j_no_errors,
    "scene_unchanged": _j_unchanged, "python": _j_python,
}


def python_exprs(checks: list[dict]) -> dict[str, str]:
    """{index: expr} for the checks Blender has to evaluate."""
    return {str(i): c["expr"] for i, c in enumerate(checks) if c["check"] == "python"}
