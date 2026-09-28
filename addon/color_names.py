"""Colour names and material words for set_color, with no bpy dependency.

A colour arrives as a name ("red", "warm white", "brushed steel"), a hex
string ("#c8a2c8", "f00") or numbers ([r, g, b], 0..1, or 0..255 when any
component is above 1). Names and hex are sRGB, the way people and colour
pickers mean them; Blender's Base Color socket is scene-linear, so the
resolved colour carries both.

Material words also carry a preset metallic/roughness (gold is metal,
concrete is rough); an explicit argument always wins over the preset.
"""

from __future__ import annotations

import difflib
import re

DEFAULT_ROUGHNESS = 0.5
DEFAULT_METALLIC = 0.0

# The CSS Color Module Level 4 named colours.
CSS_COLORS = {
    "aliceblue": "f0f8ff", "antiquewhite": "faebd7", "aqua": "00ffff",
    "aquamarine": "7fffd4", "azure": "f0ffff", "beige": "f5f5dc", "bisque": "ffe4c4",
    "black": "000000", "blanchedalmond": "ffebcd", "blue": "0000ff",
    "blueviolet": "8a2be2", "brown": "a52a2a", "burlywood": "deb887",
    "cadetblue": "5f9ea0", "chartreuse": "7fff00", "chocolate": "d2691e",
    "coral": "ff7f50", "cornflowerblue": "6495ed", "cornsilk": "fff8dc",
    "crimson": "dc143c", "cyan": "00ffff", "darkblue": "00008b", "darkcyan": "008b8b",
    "darkgoldenrod": "b8860b", "darkgray": "a9a9a9", "darkgreen": "006400",
    "darkgrey": "a9a9a9", "darkkhaki": "bdb76b", "darkmagenta": "8b008b",
    "darkolivegreen": "556b2f", "darkorange": "ff8c00", "darkorchid": "9932cc",
    "darkred": "8b0000", "darksalmon": "e9967a", "darkseagreen": "8fbc8f",
    "darkslateblue": "483d8b", "darkslategray": "2f4f4f", "darkslategrey": "2f4f4f",
    "darkturquoise": "00ced1", "darkviolet": "9400d3", "deeppink": "ff1493",
    "deepskyblue": "00bfff", "dimgray": "696969", "dimgrey": "696969",
    "dodgerblue": "1e90ff", "firebrick": "b22222", "floralwhite": "fffaf0",
    "forestgreen": "228b22", "fuchsia": "ff00ff", "gainsboro": "dcdcdc",
    "ghostwhite": "f8f8ff", "goldenrod": "daa520", "gray": "808080",
    "green": "008000", "greenyellow": "adff2f", "grey": "808080", "honeydew": "f0fff0",
    "hotpink": "ff69b4", "indianred": "cd5c5c", "indigo": "4b0082", "ivory": "fffff0",
    "khaki": "f0e68c", "lavender": "e6e6fa", "lavenderblush": "fff0f5",
    "lawngreen": "7cfc00", "lemonchiffon": "fffacd", "lightblue": "add8e6",
    "lightcoral": "f08080", "lightcyan": "e0ffff", "lightgoldenrodyellow": "fafad2",
    "lightgray": "d3d3d3", "lightgreen": "90ee90", "lightgrey": "d3d3d3",
    "lightpink": "ffb6c1", "lightsalmon": "ffa07a", "lightseagreen": "20b2aa",
    "lightskyblue": "87cefa", "lightslategray": "778899", "lightslategrey": "778899",
    "lightsteelblue": "b0c4de", "lightyellow": "ffffe0", "lime": "00ff00",
    "limegreen": "32cd32", "linen": "faf0e6", "magenta": "ff00ff", "maroon": "800000",
    "mediumaquamarine": "66cdaa", "mediumblue": "0000cd", "mediumorchid": "ba55d3",
    "mediumpurple": "9370db", "mediumseagreen": "3cb371", "mediumslateblue": "7b68ee",
    "mediumspringgreen": "00fa9a", "mediumturquoise": "48d1cc",
    "mediumvioletred": "c71585", "midnightblue": "191970", "mintcream": "f5fffa",
    "mistyrose": "ffe4e1", "moccasin": "ffe4b5", "navajowhite": "ffdead",
    "navy": "000080", "oldlace": "fdf5e6", "olive": "808000", "olivedrab": "6b8e23",
    "orange": "ffa500", "orangered": "ff4500", "orchid": "da70d6",
    "palegoldenrod": "eee8aa", "palegreen": "98fb98", "paleturquoise": "afeeee",
    "palevioletred": "db7093", "papayawhip": "ffefd5", "peachpuff": "ffdab9",
    "peru": "cd853f", "pink": "ffc0cb", "plum": "dda0dd", "powderblue": "b0e0e6",
    "purple": "800080", "rebeccapurple": "663399", "red": "ff0000",
    "rosybrown": "bc8f8f", "royalblue": "4169e1", "saddlebrown": "8b4513",
    "salmon": "fa8072", "sandybrown": "f4a460", "seagreen": "2e8b57",
    "seashell": "fff5ee", "sienna": "a0522d", "silver": "c0c0c0", "skyblue": "87ceeb",
    "slateblue": "6a5acd", "slategray": "708090", "slategrey": "708090", "snow": "fffafa",
    "springgreen": "00ff7f", "steelblue": "4682b4", "tan": "d2b48c", "teal": "008080",
    "thistle": "d8bfd8", "tomato": "ff6347", "turquoise": "40e0d0", "violet": "ee82ee",
    "wheat": "f5deb3", "white": "ffffff", "whitesmoke": "f5f5f5", "yellow": "ffff00",
    "yellowgreen": "9acd32",
}

# Material words: sRGB hex, metallic, roughness (None = the call's default).
# Metals get metallic 1 and a roughness that reads as that finish.
MATERIALS = {
    # whites and neutrals
    "warm white": ("f4ede1", None, None),
    "off white": ("f2efe6", None, None),
    "cool white": ("eef2f5", None, None),
    "pure white": ("ffffff", None, None),
    "cream": ("f3e9d2", None, None),
    "bone": ("e3dac9", None, None),
    "charcoal": ("36393b", None, None),
    "graphite": ("4a4d50", None, 0.6),
    "matte black": ("111111", None, 0.9),
    "gloss black": ("0a0a0a", None, 0.1),
    "jet black": ("0a0a0a", None, None),
    # woods
    "walnut": ("5c3d2e", None, 0.6),
    "oak": ("b58a58", None, 0.6),
    "light oak": ("c9a877", None, 0.6),
    "maple": ("d9b98c", None, 0.55),
    "cherry": ("8e4a32", None, 0.55),
    "mahogany": ("6b2e21", None, 0.55),
    "pine": ("d8b67c", None, 0.65),
    "birch": ("e0cda7", None, 0.6),
    "teak": ("a0703f", None, 0.6),
    "ebony": ("2b2320", None, 0.5),
    "bamboo": ("d6bf86", None, 0.55),
    "plywood": ("d1b48a", None, 0.7),
    # metals
    "gold": ("ffd27a", 1.0, 0.25),
    "polished gold": ("ffd27a", 1.0, 0.1),
    "rose gold": ("f4bfa6", 1.0, 0.25),
    "brass": ("e1c16e", 1.0, 0.3),
    "copper": ("f2a385", 1.0, 0.3),
    "bronze": ("cd8f55", 1.0, 0.35),
    "chrome": ("f0f0f2", 1.0, 0.05),
    "silver": ("f5f5f5", 1.0, 0.15),
    "steel": ("c6c9cc", 1.0, 0.35),
    "stainless steel": ("cfd2d4", 1.0, 0.3),
    "brushed steel": ("c3c6c8", 1.0, 0.45),
    "brushed aluminium": ("d9dbdc", 1.0, 0.45),
    "aluminium": ("e3e5e6", 1.0, 0.3),
    "aluminum": ("e3e5e6", 1.0, 0.3),
    "iron": ("a6a6a6", 1.0, 0.5),
    "cast iron": ("4d4d4d", 1.0, 0.7),
    "nickel": ("dcd6c8", 1.0, 0.25),
    "titanium": ("c2bdb3", 1.0, 0.35),
    "zinc": ("bcc3c7", 1.0, 0.4),
    "gunmetal": ("53585c", 1.0, 0.4),
    # stone, masonry, ceramics
    "concrete": ("a3a19b", None, 0.9),
    "polished concrete": ("aeaca6", None, 0.4),
    "cement": ("9e9d98", None, 0.9),
    "quartz": ("eeeae3", None, 0.2),
    "white quartz": ("f3f1ec", None, 0.2),
    "warm white quartz": ("f4ede1", None, 0.2),
    "marble": ("ece9e4", None, 0.15),
    "white marble": ("f2f0ec", None, 0.15),
    "black marble": ("1f1f22", None, 0.15),
    "granite": ("777673", None, 0.35),
    "black granite": ("2a2a2b", None, 0.3),
    "slate": ("5a6166", None, 0.6),
    "limestone": ("d8cfb8", None, 0.8),
    "sandstone": ("d2b48c", None, 0.85),
    "terracotta": ("c4623f", None, 0.8),
    "brick": ("a34f3b", None, 0.85),
    "red brick": ("a34f3b", None, 0.85),
    "clay": ("b86f50", None, 0.85),
    "porcelain": ("f5f4f0", None, 0.1),
    "ceramic": ("f0eee9", None, 0.15),
    "stucco": ("e6dccb", None, 0.9),
    "plaster": ("ece6da", None, 0.85),
    "asphalt": ("3a3a3a", None, 0.95),
    # other everyday materials
    "sand": ("d8c39a", None, 0.95),
    "grass": ("4f7a2a", None, 0.9),
    "leather": ("6e4128", None, 0.55),
    "tan leather": ("a8744a", None, 0.55),
    "rubber": ("1c1c1c", None, 0.9),
    "cork": ("b48a5e", None, 0.9),
    "denim": ("3b5a7d", None, 0.8),
    "linen fabric": ("e8e0cf", None, 0.9),
    "felt": ("6a6a6a", None, 1.0),
    "glass": ("f5f8f8", None, 0.05),
    "plastic": ("f0f0f0", None, 0.4),
}

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def key(name: str) -> str:
    """Lookup key: lowercase letters and digits only ("Off-White" -> "offwhite")."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _pretty(name: str) -> str:
    return " ".join(w.capitalize() for w in name.split())


# key -> (display name, hex, metallic, roughness). Material words win over CSS.
_TABLE: dict[str, tuple[str, str, float | None, float | None]] = {}
for _n, _h in CSS_COLORS.items():
    _TABLE[_n] = (_n, _h, None, None)
for _n, (_h, _m, _r) in MATERIALS.items():
    _TABLE[key(_n)] = (_n, _h, _m, _r)
# CSS "gold" and "silver" are paint colours; the material word is what people mean.


def names() -> list[str]:
    return sorted(v[0] for v in _TABLE.values())


def hex_to_srgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def srgb_to_linear(c: float) -> float:
    c = max(0.0, min(1.0, float(c)))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, float(c)))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def to_hex(srgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in srgb)


def suggest(name: str, n: int = 5) -> list[str]:
    k = key(name)
    hits = difflib.get_close_matches(k, list(_TABLE), n=n, cutoff=0.6)
    # Substring matches too: "walnut wood" should suggest walnut.
    hits += [t for t in _TABLE if t not in hits and len(t) > 2 and (t in k or k in t)]
    return [_TABLE[h][0] for h in hits[:n]]


def lookup(name: str):
    """(display name, sRGB, metallic, roughness) for a colour name or hex, or None."""
    m = _HEX.match(str(name).strip())
    if m:
        return (to_hex(hex_to_srgb(m.group(1))), hex_to_srgb(m.group(1)), None, None)
    k = key(name)
    if k in _TABLE:
        disp, h, met, rough = _TABLE[k]
        return (disp, hex_to_srgb(h), met, rough)
    # Common suffixes: "red paint", "walnut wood", "oak finish".
    for suffix in ("wood", "paint", "finish", "metal", "colour", "color", "stone", "material"):
        if k.endswith(suffix) and k[: -len(suffix)] in _TABLE:
            disp, h, met, rough = _TABLE[k[: -len(suffix)]]
            return (disp, hex_to_srgb(h), met, rough)
    return None


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _unit(value, what: str) -> float | None:
    if value is None:
        return None
    if not _number(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{what} must be a number from 0 to 1")
    return float(value)


def resolve(color, roughness=None, metallic=None) -> dict:
    """Everything set_color applies, or ValueError with suggestions.

    Returns {"label", "srgb", "linear", "hex", "roughness", "metallic",
    "preset"} where preset says whether the name set metallic/roughness.
    """
    rough = _unit(roughness, "roughness")
    metal = _unit(metallic, "metallic")
    preset_m = preset_r = None
    if isinstance(color, str):
        found = lookup(color)
        if found is None:
            close = suggest(color)
            hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            raise ValueError(f"unknown colour {color!r}{hint} (or pass [r, g, b] from 0 to 1, "
                             "or a hex code like '#d8c39a')")
        label, srgb, preset_m, preset_r = found
    elif isinstance(color, (list, tuple)) and len(color) in (3, 4) and all(_number(c) for c in color):
        vals = [float(c) for c in color[:3]]
        if any(v < 0 for v in vals):
            raise ValueError("colour components can't be negative")
        if any(v > 1 for v in vals):
            if any(v > 255 for v in vals):
                raise ValueError("colour components must be 0 to 1 (or 0 to 255)")
            vals = [v / 255.0 for v in vals]
        srgb = tuple(vals)
        label = to_hex(srgb)
    else:
        raise ValueError("color must be a name ('red', 'warm white', 'brass'), a hex code, "
                         "or [r, g, b] from 0 to 1")
    final_m = metal if metal is not None else (preset_m if preset_m is not None else DEFAULT_METALLIC)
    final_r = rough if rough is not None else (preset_r if preset_r is not None else DEFAULT_ROUGHNESS)
    return {
        "label": label,
        "srgb": [round(c, 4) for c in srgb],
        "linear": [round(srgb_to_linear(c), 5) for c in srgb],
        "hex": to_hex(srgb),
        "roughness": final_r,
        "metallic": final_m,
        "preset": preset_m is not None or preset_r is not None,
    }


def material_name(resolved: dict) -> str:
    """Default material name for a resolved colour: "Warm White", "Brass", "#1a2b3c"."""
    label = resolved["label"]
    return label if label.startswith("#") else _pretty(label)
