"""Node-building and face-selection helpers shared by the Poly Haven commands.

Kept out of polyhaven.py so the handlers stay readable. Everything here
needs bpy; the pure maths lives in addon/polyhaven_helpers.py.
"""

from __future__ import annotations

import math
from contextlib import suppress

import bpy
import requests

from ...constants import REQ_HEADERS
from ...polyhaven_helpers import (
    classify_map,
    dimensions_m,
    height_matches,
    mapping_scale,
    normal_matches,
    pick_maps,
)


def fetch_dimensions_m(asset_id):
    """Real-world texture size (w, h) in metres from Poly Haven /info, or None."""
    try:
        r = requests.get(f"https://api.polyhaven.com/info/{asset_id}", headers=REQ_HEADERS, timeout=15)
        if r.status_code == 200:
            return dimensions_m(r.json())
    except (requests.RequestException, ValueError) as e:
        print(f"[BlenderMCP] Poly Haven /info/{asset_id} failed: {e}")
    return None


def find_texture_images(texture_id):
    """Images downloaded for ``texture_id``, keyed by Poly Haven map name.

    download_polyhaven_asset names them "<id>_<map>.<ext>", so the map is
    the name minus the "<id>_" prefix and the extension ("nor_gl", "arm").
    """
    prefix = f"{texture_id}_"
    found = {}
    for img in bpy.data.images:
        if img.name.startswith(prefix):
            key = img.name[len(prefix):].rsplit(".", 1)[0]
            if classify_map(key) is not None and key not in found:
                found[key] = img
    return found


def stored_dimensions_m(images_by_key):
    """Real-world size recorded on the images at download time, if any."""
    for img in images_by_key.values():
        dims = img.get("polyhaven_dimensions_m") if hasattr(img, "get") else None
        if dims and len(dims) >= 2:
            return (float(dims[0]), float(dims[1]))
    return None


def _mix_rgba_multiply(nodes, links, a_socket, b_socket, factor, location):
    """Multiply two colours with the 4.x/5.x Mix node; returns its output."""
    mix = nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    mix.location = location
    mix.inputs["Factor"].default_value = factor
    links.new(a_socket, mix.inputs[6])  # A_Color
    links.new(b_socket, mix.inputs[7])  # B_Color
    return mix.outputs[2]  # Result_Color


def build_pbr_material(
    name, images_by_key, coordinates="uv", tile_size_m=None, obj=None,
    projection_blend=0.2, displacement_scale=0.02, ao_strength=0.8,
):
    """Principled material from Poly Haven maps. Returns (material, wired).

    coordinates:
      "uv"     UV map, mapping scale 1 (real-world size depends on the unwrap)
      "object" Object coordinates with box projection; scale = object_scale /
               tile, so the texture repeats every tile metres on the object
               and moves with it
      "world"  world position with box projection; shareable between objects,
               but the texture stays put in the world if the object moves
    """
    roles = pick_maps(images_by_key.keys())
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (700, 0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (400, 0)
    links.new(principled.outputs[0], output.inputs["Surface"])

    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-700, 0)
    if coordinates == "world":
        geo = nodes.new("ShaderNodeNewGeometry")
        geo.location = (-900, 0)
        links.new(geo.outputs["Position"], mapping.inputs["Vector"])
    else:
        tex_coord = nodes.new("ShaderNodeTexCoord")
        tex_coord.location = (-900, 0)
        links.new(
            tex_coord.outputs["Object" if coordinates == "object" else "UV"],
            mapping.inputs["Vector"],
        )

    scale_used = None
    if tile_size_m is not None and coordinates in ("object", "world"):
        obj_scale = tuple(obj.scale) if (obj is not None and coordinates == "object") else (1.0, 1.0, 1.0)
        scale_used = mapping_scale(tile_size_m, obj_scale)
        mapping.inputs["Scale"].default_value = scale_used

    box = coordinates in ("object", "world")
    tex_nodes = {}
    y = 400
    for role in ("base", "rough", "metal", "normal_pick", "disp", "ao", "arm"):
        key = roles.get(role)
        if not key:
            continue
        img = images_by_key[key]
        with suppress(Exception):
            img.colorspace_settings.name = "sRGB" if role == "base" else "Non-Color"
        node = nodes.new("ShaderNodeTexImage")
        node.image = img
        node.location = (-400, y)
        y -= 280
        if box:
            node.projection = "BOX"
            node.projection_blend = projection_blend
        links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        tex_nodes[role] = node

    wired = {}
    arm_sep = None
    if "arm" in tex_nodes:
        arm_sep = nodes.new("ShaderNodeSeparateColor")
        arm_sep.location = (-100, -200)
        links.new(tex_nodes["arm"].outputs["Color"], arm_sep.inputs["Color"])

    if "base" in tex_nodes:
        base_out = tex_nodes["base"].outputs["Color"]
        ao_src = None
        if "ao" in tex_nodes:
            ao_src, wired["ao"] = tex_nodes["ao"].outputs["Color"], roles["ao"]
        elif arm_sep is not None:
            ao_src, wired["ao"] = arm_sep.outputs["Red"], f"{roles['arm']}.R"
        if ao_src is not None:
            base_out = _mix_rgba_multiply(nodes, links, base_out, ao_src, ao_strength, (100, 300))
        links.new(base_out, principled.inputs["Base Color"])
        wired["base"] = roles["base"]

    if "rough" in tex_nodes:
        links.new(tex_nodes["rough"].outputs["Color"], principled.inputs["Roughness"])
        wired["rough"] = roles["rough"]
    elif arm_sep is not None:
        links.new(arm_sep.outputs["Green"], principled.inputs["Roughness"])
        wired["rough"] = f"{roles['arm']}.G"

    if "metal" in tex_nodes:
        links.new(tex_nodes["metal"].outputs["Color"], principled.inputs["Metallic"])
        wired["metal"] = roles["metal"]
    elif arm_sep is not None:
        links.new(arm_sep.outputs["Blue"], principled.inputs["Metallic"])
        wired["metal"] = f"{roles['arm']}.B"

    if "normal_pick" in tex_nodes:
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (100, -100)
        links.new(tex_nodes["normal_pick"].outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], principled.inputs["Normal"])
        wired["normal"] = roles["normal_pick"]

    if "disp" in tex_nodes:
        disp = nodes.new("ShaderNodeDisplacement")
        disp.location = (400, -350)
        disp.inputs["Scale"].default_value = displacement_scale
        disp.inputs["Midlevel"].default_value = 0.5
        links.new(tex_nodes["disp"].outputs["Color"], disp.inputs["Height"])
        links.new(disp.outputs["Displacement"], output.inputs["Displacement"])
        wired["disp"] = roles["disp"]

    wired["_coordinates"] = coordinates
    wired["_mapping_scale"] = [round(v, 6) for v in scale_used] if scale_used else None
    return mat, wired


# Labels on the world nodes set_world_hdri creates, so it can find them again.
HDRI_MIX_LABEL = "BlenderMCP camera mix"
HDRI_CAMERA_BG_LABEL = "BlenderMCP camera background"
HDRI_CAMERA_PATH_LABEL = "BlenderMCP camera ray"


def _world_nodes(world):
    nt = world.node_tree
    env = next((n for n in nt.nodes if n.type == "TEX_ENVIRONMENT"), None)
    out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD" and n.is_active_output), None)
    if out is None:
        out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None)
    bg = None
    if env is not None:
        for link in env.outputs["Color"].links:
            if link.to_node.type == "BACKGROUND":
                bg = link.to_node
                break
    return nt, env, bg, out


def apply_world_hdri(world, rotation_deg=None, strength=None, background_visible=None,
                     background_color=(0.05, 0.05, 0.05)):
    """Set HDRI Z rotation, Background strength and camera visibility in place.

    Rotation goes through a Mapping node between the texture coordinates and
    the Environment Texture (added if missing). Hiding the HDRI from the
    camera keeps its lighting and reflections: a Light Path "Is Camera Ray"
    switch shows a plain colour to the camera only, which works in EEVEE and
    Cycles alike, unlike film transparency.
    """
    world.use_nodes = True
    nt, env, bg, out = _world_nodes(world)
    if env is None or bg is None or out is None:
        raise ValueError(
            "The world has no Environment Texture feeding a Background node. "
            "Import an HDRI first with blender_download_polyhaven_asset(asset_type='hdris')."
        )
    links = nt.links

    mapping = None
    if env.inputs["Vector"].links:
        src = env.inputs["Vector"].links[0].from_node
        if src.type == "MAPPING":
            mapping = src
    if mapping is None:
        coord = nt.nodes.new("ShaderNodeTexCoord")
        coord.location = (env.location.x - 400, env.location.y)
        mapping = nt.nodes.new("ShaderNodeMapping")
        mapping.location = (env.location.x - 200, env.location.y)
        links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    if rotation_deg is not None:
        rot = mapping.inputs["Rotation"].default_value
        mapping.inputs["Rotation"].default_value = (rot[0], rot[1], math.radians(float(rotation_deg)))
    if strength is not None:
        bg.inputs["Strength"].default_value = float(strength)

    mix = next((n for n in nt.nodes if n.label == HDRI_MIX_LABEL), None)
    if background_visible is False and mix is None:
        cam_bg = nt.nodes.new("ShaderNodeBackground")
        cam_bg.label = HDRI_CAMERA_BG_LABEL
        cam_bg.location = (bg.location.x, bg.location.y - 200)
        cam_bg.inputs["Color"].default_value = (*tuple(background_color)[:3], 1.0)
        light_path = nt.nodes.new("ShaderNodeLightPath")
        light_path.label = HDRI_CAMERA_PATH_LABEL
        light_path.location = (bg.location.x, bg.location.y + 300)
        mix = nt.nodes.new("ShaderNodeMixShader")
        mix.label = HDRI_MIX_LABEL
        mix.location = (bg.location.x + 200, bg.location.y)
        links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
        links.new(bg.outputs["Background"], mix.inputs[1])
        links.new(cam_bg.outputs["Background"], mix.inputs[2])
        links.new(mix.outputs["Shader"], out.inputs["Surface"])
    elif background_visible is True and mix is not None:
        links.new(bg.outputs["Background"], out.inputs["Surface"])
        for n in list(nt.nodes):
            if n.label in (HDRI_MIX_LABEL, HDRI_CAMERA_BG_LABEL, HDRI_CAMERA_PATH_LABEL):
                nt.nodes.remove(n)
        mix = None

    return {
        "world": world.name,
        "hdri": env.image.name if env.image else None,
        "rotation_deg": round(math.degrees(mapping.inputs["Rotation"].default_value[2]), 3),
        "strength": round(bg.inputs["Strength"].default_value, 4),
        "background_visible": mix is None,
    }


def select_faces(obj, faces=None, normal=None, normal_tolerance_deg=30.0,
                 min_z=None, max_z=None, slot=None, attribute=None):
    """Indices of polygons matching every given rule (AND). No rule = all faces."""
    mesh = obj.data
    mw = obj.matrix_world
    nmat = mw.to_3x3().inverted_safe().transposed()
    index_set = {int(i) for i in faces} if faces is not None else None
    attr = None
    if attribute:
        attr = mesh.attributes.get(attribute)
        if attr is None or attr.domain != "FACE":
            raise ValueError(f"No face-domain attribute named {attribute!r} on {obj.name}")
    selected = []
    for p in mesh.polygons:
        if index_set is not None and p.index not in index_set:
            continue
        if slot is not None and p.material_index != int(slot):
            continue
        if normal is not None:
            wn = nmat @ p.normal
            if not normal_matches(tuple(wn), normal, normal_tolerance_deg):
                continue
        if (min_z is not None or max_z is not None) and not height_matches((mw @ p.center).z, min_z, max_z):
            continue
        if attr is not None and not bool(attr.data[p.index].value):
            continue
        selected.append(p.index)
    return selected
