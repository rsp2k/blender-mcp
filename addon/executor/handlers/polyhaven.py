"""Poly Haven asset integration handlers.

Search/categories metadata, plus large download_polyhaven_asset that
handles HDRIs (world environment setup), textures (material with
roughness/metallic/normal/displacement/AO node graph), and models
(GLTF/FBX/OBJ/BLEND import).

`set_texture` applies a previously downloaded texture to a specific
object — covers ARM packing and AO multiplication into base color.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import traceback
from contextlib import suppress

import bpy
import requests

from ...constants import REQ_HEADERS
from ...preferences import get_prefs
from ...polyhaven_helpers import (
    POLYHAVEN_DISABLED_HINT,
    classify_map,
    dimensions_m,
)
from ..registry import command
from ._polyhaven_nodes import (
    apply_world_hdri as _apply_world_hdri,
    build_pbr_material as _build_pbr_material,
    fetch_dimensions_m as _fetch_dimensions_m,
    find_texture_images,
    select_faces,
    stored_dimensions_m,
)

# Gate predicate shared by the gated polyhaven handlers; defined once
# so all four decorators reference the same callable. Phase 8 changed
# the gate signature from (scene) to (prefs).
_polyhaven_enabled = lambda prefs: prefs.use_polyhaven  # noqa: E731


class PolyhavenHandlersMixin:
    """`get_polyhaven_status`, `get_polyhaven_categories`,
    `search_polyhaven_assets`, `download_polyhaven_asset`, `set_texture`.
    """

    @command("get_polyhaven_categories", gate=_polyhaven_enabled, disabled_hint=POLYHAVEN_DISABLED_HINT)
    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            response = requests.get(f"https://api.polyhaven.com/categories/{asset_type}", headers=REQ_HEADERS)
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    @command("search_polyhaven_assets", gate=_polyhaven_enabled, disabled_hint=POLYHAVEN_DISABLED_HINT)
    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS)
            if response.status_code == 200:
                # Limit the response size to avoid overwhelming Blender
                assets = response.json()
                # Return only the first 20 assets to keep response size manageable
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:  # Limit to 20 assets
                        break
                    if isinstance(value, dict):
                        # Real-world size in metres (textures only; HDRIs have none).
                        value = dict(value, dimensions_m=dimensions_m(value))
                    limited_assets[key] = value

                return {"assets": limited_assets, "total_count": len(assets), "returned_count": len(limited_assets)}
            else:
                return {"error": f"API request failed with status code {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    @command("download_polyhaven_asset", gate=_polyhaven_enabled, disabled_hint=POLYHAVEN_DISABLED_HINT)
    def download_polyhaven_asset(
        self, asset_id, asset_type, resolution="1k", file_format=None,
        rotation_deg=None, strength=None, background_visible=None,
    ):
        try:
            # First get the files information
            files_response = requests.get(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)
            if files_response.status_code != 200:
                return {"error": f"Failed to get asset files: {files_response.status_code}"}

            files_data = files_response.json()

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs

                if "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]:
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]

                    # For HDRIs, we need to save to a temporary file first
                    # since Blender can't properly load HDR data directly from memory
                    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                        # Download the file
                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download HDRI: {response.status_code}"}

                        tmp_file.write(response.content)
                        tmp_path = tmp_file.name

                    try:
                        # Create a new world if none exists
                        if not bpy.data.worlds:
                            bpy.data.worlds.new("World")

                        world = bpy.data.worlds[0]
                        world.use_nodes = True
                        node_tree = world.node_tree

                        # Clear existing nodes
                        for node in node_tree.nodes:
                            node_tree.nodes.remove(node)

                        # Create nodes
                        tex_coord = node_tree.nodes.new(type='ShaderNodeTexCoord')
                        tex_coord.location = (-800, 0)

                        mapping = node_tree.nodes.new(type='ShaderNodeMapping')
                        mapping.location = (-600, 0)

                        # Load the image from the temporary file
                        env_tex = node_tree.nodes.new(type='ShaderNodeTexEnvironment')
                        env_tex.location = (-400, 0)
                        env_tex.image = bpy.data.images.load(tmp_path)
                        env_tex.image.name = f"{asset_id}_{resolution}.{file_format}"

                        # Use a color space that exists in all Blender versions
                        if file_format.lower() == 'exr':
                            # Try to use Linear color space for EXR files
                            try:
                                env_tex.image.colorspace_settings.name = 'Linear'
                            except Exception:
                                # Fallback to Non-Color if Linear isn't available
                                env_tex.image.colorspace_settings.name = 'Non-Color'
                        else:  # hdr
                            # For HDR files, try these options in order
                            for color_space in ['Linear', 'Linear Rec.709', 'Non-Color']:
                                try:
                                    env_tex.image.colorspace_settings.name = color_space
                                    break  # Stop if we successfully set a color space
                                except Exception:
                                    continue

                        background = node_tree.nodes.new(type='ShaderNodeBackground')
                        background.location = (-200, 0)

                        output = node_tree.nodes.new(type='ShaderNodeOutputWorld')
                        output.location = (0, 0)

                        # Connect nodes
                        node_tree.links.new(tex_coord.outputs['Generated'], mapping.inputs['Vector'])
                        node_tree.links.new(mapping.outputs['Vector'], env_tex.inputs['Vector'])
                        node_tree.links.new(env_tex.outputs['Color'], background.inputs['Color'])
                        node_tree.links.new(background.outputs['Background'], output.inputs['Surface'])

                        # Set as active world
                        bpy.context.scene.world = world

                        # Keep the pixels inside the .blend: the temp file is
                        # removed below, and an unpacked image would come up
                        # pink on the next file open.
                        with suppress(Exception):
                            env_tex.image.pack()
                        with suppress(Exception):
                            os.unlink(tmp_path)

                        world_state = _apply_world_hdri(
                            world, rotation_deg=rotation_deg, strength=strength,
                            background_visible=background_visible,
                        )
                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": env_tex.image.name,
                            "world": world_state,
                            "hint": "Adjust later with blender_set_world_hdri(rotation_deg, strength, background_visible).",
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {str(e)}"}
                else:
                    return {"error": "Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}
                tex_dims = _fetch_dimensions_m(asset_id)

                try:
                    for map_type in files_data:
                        if classify_map(map_type) is not None:  # skip blend/gltf/mtlx
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    # Download the file
                                    response = requests.get(file_url, headers=REQ_HEADERS)
                                    if response.status_code == 200:
                                        tmp_file.write(response.content)
                                        tmp_path = tmp_file.name

                                        # Load image from temporary file
                                        image = bpy.data.images.load(tmp_path)
                                        image.name = f"{asset_id}_{map_type}.{file_format}"

                                        # Pack the image into .blend file
                                        image.pack()
                                        if tex_dims:
                                            image["polyhaven_dimensions_m"] = list(tex_dims)

                                        # Set color space based on map type
                                        if map_type in ['color', 'diffuse', 'albedo']:
                                            try:
                                                image.colorspace_settings.name = 'sRGB'
                                            except Exception:
                                                pass
                                        else:
                                            try:
                                                image.colorspace_settings.name = 'Non-Color'
                                            except Exception:
                                                pass

                                        downloaded_maps[map_type] = image

                                        # Clean up temporary file
                                        try:
                                            os.unlink(tmp_path)
                                        except Exception:
                                            pass

                    if not downloaded_maps:
                        return {"error": "No texture maps found for the requested resolution and format"}

                    # Build the material with the shared builder: UV mapping, as
                    # before, but maps are wired by role so normal (nor_gl/nor_dx)
                    # and displacement actually connect.
                    mat, wired = _build_pbr_material(
                        asset_id, downloaded_maps, coordinates="uv", tile_size_m=None, obj=None,
                    )
                    if tex_dims:
                        mat["polyhaven_dimensions_m"] = list(tex_dims)
                    mat["polyhaven_id"] = asset_id

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": list(downloaded_maps.keys()),
                        "wired": wired,
                        "dimensions_m": list(tex_dims) if tex_dims else None,
                        "hint": (
                            "For real-world scale use blender_make_pbr_material(texture_id, "
                            "object_name=...) (box projection sized from dimensions_m), then "
                            "blender_assign_material to put it on chosen faces."
                        ),
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {str(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        response = requests.get(file_url, headers=REQ_HEADERS)
                        if response.status_code != 200:
                            return {"error": f"Failed to download model: {response.status_code}"}

                        with open(main_file_path, "wb") as f:
                            f.write(response.content)

                        # Check for included files and download them
                        if "include" in file_info and file_info["include"]:
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Create the directory structure for the included file
                                include_file_path = os.path.join(temp_dir, include_path)
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                include_response = requests.get(include_url, headers=REQ_HEADERS)
                                if include_response.status_code == 200:
                                    with open(include_file_path, "wb") as f:
                                        f.write(include_response.content)
                                else:
                                    print(f"Failed to download included file: {include_path}")

                        # Import the model into Blender
                        if file_format == "gltf" or file_format == "glb":
                            bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            bpy.ops.import_scene.obj(filepath=main_file_path)
                        elif file_format == "blend":
                            # For blend files, we need to append or link
                            with bpy.data.libraries.load(main_file_path, link=False) as (data_from, data_to):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}

                        # Get the names of imported objects
                        imported_objects = [obj.name for obj in bpy.context.selected_objects]

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects
                        }
                    except Exception as e:
                        return {"error": f"Failed to import model: {str(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": "Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    @command("set_texture", gate=_polyhaven_enabled, disabled_hint=POLYHAVEN_DISABLED_HINT)
    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object by creating a new material"""
        try:
            # Get the object
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            # Make sure object can accept materials
            if not hasattr(obj, 'data') or not hasattr(obj.data, 'materials'):
                return {"error": f"Object {object_name} cannot accept materials"}

            # Find all images related to this texture and ensure they're properly loaded
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    # Extract the map type from the image name
                    map_type = img.name.split('_')[-1].split('.')[0]

                    # Force a reload of the image
                    img.reload()

                    # Ensure proper color space
                    if map_type.lower() in ['color', 'diffuse', 'albedo']:
                        try:
                            img.colorspace_settings.name = 'sRGB'
                        except Exception:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = 'Non-Color'
                        except Exception:
                            pass

                    # Ensure the image is packed
                    if not img.packed_file:
                        img.pack()

                    texture_images[map_type] = img
                    print(f"Loaded texture map: {map_type} - {img.name}")

                    # Debug info
                    print(f"Image size: {img.size[0]}x{img.size[1]}")
                    print(f"Color space: {img.colorspace_settings.name}")
                    print(f"File format: {img.file_format}")
                    print(f"Is packed: {bool(img.packed_file)}")

            if not texture_images:
                return {"error": f"No texture images found for: {texture_id}. Please download the texture first."}

            # Create a new material
            new_mat_name = f"{texture_id}_material_{object_name}"

            # Remove any existing material with this name to avoid conflicts
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True

            # Set up the material nodes
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links

            # Clear default nodes
            nodes.clear()

            # Create output node
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (600, 0)

            # Create principled BSDF node
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            # Add texture nodes based on available maps
            tex_coord = nodes.new(type='ShaderNodeTexCoord')
            tex_coord.location = (-800, 0)

            mapping = nodes.new(type='ShaderNodeMapping')
            mapping.location = (-600, 0)
            mapping.vector_type = 'TEXTURE'  # Changed from default 'POINT' to 'TEXTURE'
            links.new(tex_coord.outputs['UV'], mapping.inputs['Vector'])

            # Position offset for texture nodes
            x_pos = -400
            y_pos = 300

            # Connect different texture maps (first pass)
            for map_type, image in texture_images.items():
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image

                # Set color space based on map type
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    try:
                        tex_node.image.colorspace_settings.name = 'sRGB'
                    except Exception:
                        pass
                else:
                    try:
                        tex_node.image.colorspace_settings.name = 'Non-Color'
                    except Exception:
                        pass

                links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])

                # Connect to appropriate input on Principled BSDF
                if map_type.lower() in ['color', 'diffuse', 'albedo']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif map_type.lower() in ['roughness', 'rough']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                elif map_type.lower() in ['metallic', 'metalness', 'metal']:
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif map_type.lower() in ['normal', 'nor', 'dx', 'gl']:
                    # Add normal map node
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif map_type.lower() in ['displacement', 'disp', 'height']:
                    # Add displacement node
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(tex_node.outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])

                y_pos -= 250

            # Second pass: Connect nodes with proper handling for special cases
            texture_nodes = {}

            # First find all texture nodes and store them by map type
            for node in nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    for map_type, image in texture_images.items():
                        if node.image == image:
                            texture_nodes[map_type] = node
                            break

            # Now connect everything using the nodes instead of images
            # Handle base color (diffuse)
            for map_name in ['color', 'diffuse', 'albedo']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Base Color'])
                    print(f"Connected {map_name} to Base Color")
                    break

            # Handle roughness
            for map_name in ['roughness', 'rough']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Roughness'])
                    print(f"Connected {map_name} to Roughness")
                    break

            # Handle metallic
            for map_name in ['metallic', 'metalness', 'metal']:
                if map_name in texture_nodes:
                    links.new(texture_nodes[map_name].outputs['Color'], principled.inputs['Metallic'])
                    print(f"Connected {map_name} to Metallic")
                    break

            # Handle normal maps
            for map_name in ['gl', 'dx', 'nor']:
                if map_name in texture_nodes:
                    normal_map_node = nodes.new(type='ShaderNodeNormalMap')
                    normal_map_node.location = (100, 100)
                    links.new(texture_nodes[map_name].outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                    print(f"Connected {map_name} to Normal")
                    break

            # Handle displacement
            for map_name in ['displacement', 'disp', 'height']:
                if map_name in texture_nodes:
                    disp_node = nodes.new(type='ShaderNodeDisplacement')
                    disp_node.location = (300, -200)
                    disp_node.inputs['Scale'].default_value = 0.1  # Reduce displacement strength
                    links.new(texture_nodes[map_name].outputs['Color'], disp_node.inputs['Height'])
                    links.new(disp_node.outputs['Displacement'], output.inputs['Displacement'])
                    print(f"Connected {map_name} to Displacement")
                    break

            # Handle ARM texture (Ambient Occlusion, Roughness, Metallic)
            if 'arm' in texture_nodes:
                separate_rgb = nodes.new(type='ShaderNodeSeparateColor')
                separate_rgb.location = (-200, -100)
                links.new(texture_nodes['arm'].outputs['Color'], separate_rgb.inputs[0])

                # Connect Roughness (G) if no dedicated roughness map
                if not any(map_name in texture_nodes for map_name in ['roughness', 'rough']):
                    links.new(separate_rgb.outputs[1], principled.inputs['Roughness'])
                    print("Connected ARM.G to Roughness")

                # Connect Metallic (B) if no dedicated metallic map
                if not any(map_name in texture_nodes for map_name in ['metallic', 'metalness', 'metal']):
                    links.new(separate_rgb.outputs[2], principled.inputs['Metallic'])
                    print("Connected ARM.B to Metallic")

                # For AO (R channel), multiply with base color if we have one
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs[0].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(separate_rgb.outputs[0], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected ARM.R to AO mix with Base Color")

            # Handle AO (Ambient Occlusion) if separate
            if 'ao' in texture_nodes:
                base_color_node = None
                for map_name in ['color', 'diffuse', 'albedo']:
                    if map_name in texture_nodes:
                        base_color_node = texture_nodes[map_name]
                        break

                if base_color_node:
                    mix_node = nodes.new(type='ShaderNodeMixRGB')
                    mix_node.location = (100, 200)
                    mix_node.blend_type = 'MULTIPLY'
                    mix_node.inputs[0].default_value = 0.8  # 80% influence

                    # Disconnect direct connection to base color
                    for link in base_color_node.outputs['Color'].links:
                        if link.to_socket == principled.inputs['Base Color']:
                            links.remove(link)

                    # Connect through the mix node
                    links.new(base_color_node.outputs['Color'], mix_node.inputs[1])
                    links.new(texture_nodes['ao'].outputs['Color'], mix_node.inputs[2])
                    links.new(mix_node.outputs['Color'], principled.inputs['Base Color'])
                    print("Connected AO to mix with Base Color")

            # CRITICAL: Make sure to clear all existing materials from the object
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)

            # Assign the new material to the object
            obj.data.materials.append(new_mat)

            # CRITICAL: Make the object active and select it
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)

            # CRITICAL: Force Blender to update the material
            bpy.context.view_layer.update()

            # Get the list of texture maps
            texture_maps = list(texture_images.keys())

            # Get info about texture nodes for debugging
            material_info = {
                "name": new_mat.name,
                "has_nodes": new_mat.use_nodes,
                "node_count": len(new_mat.node_tree.nodes),
                "texture_nodes": []
            }

            for node in new_mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    connections = []
                    for output in node.outputs:
                        for link in output.links:
                            connections.append(f"{output.name} → {link.to_node.name}.{link.to_socket.name}")

                    material_info["texture_nodes"].append({
                        "name": node.name,
                        "image": node.image.name,
                        "colorspace": node.image.colorspace_settings.name,
                        "connections": connections
                    })

            return {
                "success": True,
                "message": f"Created new material and applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": texture_maps,
                "material_info": material_info
            }

        except Exception as e:
            print(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}

    # The three commands below work on data already in the file, so they are
    # not gated on use_polyhaven: nothing here talks to the network.

    @command("make_pbr_material")
    def make_pbr_material(
        self, texture_id, name=None, tile_size_m=None, coordinates="object",
        object_name=None, displacement_scale=0.02,
    ):
        """Build a real-world-scaled material from downloaded Poly Haven maps."""
        if coordinates not in ("object", "world", "uv"):
            raise ValueError("coordinates must be 'object', 'world' or 'uv'")
        images = find_texture_images(texture_id)
        if not images:
            raise ValueError(
                f"No downloaded maps for {texture_id!r}. Run "
                f"blender_download_polyhaven_asset('{texture_id}', 'textures') first."
            )
        obj = None
        if object_name:
            obj = bpy.data.objects.get(object_name)
            if obj is None:
                raise ValueError(f"Object not found: {object_name}")
        tile_source = "argument"
        if tile_size_m is None:
            tile_size_m = stored_dimensions_m(images)
            tile_source = "polyhaven dimensions" if tile_size_m else None
        if tile_size_m is None and coordinates != "uv":
            tile_size_m, tile_source = 1.0, "default 1 m (Poly Haven lists no dimensions)"

        mat, wired = _build_pbr_material(
            name or f"{texture_id}_{coordinates}", images,
            coordinates=coordinates, tile_size_m=tile_size_m, obj=obj,
            displacement_scale=displacement_scale,
        )
        mat["polyhaven_id"] = texture_id
        mat["blendermcp_coordinates"] = coordinates
        if tile_size_m is not None:
            tile = [float(tile_size_m)] * 2 if isinstance(tile_size_m, (int, float)) else [float(v) for v in tile_size_m]
            mat["blendermcp_tile_size_m"] = tile
        note = None
        if coordinates == "object" and obj is None:
            note = ("No object_name given, so the mapping assumes scale (1, 1, 1). "
                    "Pass object_name, or apply scale on the target, for exact size.")
        elif coordinates == "object":
            note = ("Mapping is baked for this object's current scale; rebuild the "
                    "material (or apply scale) if the object is rescaled.")
        return {
            "material": mat.name,
            "coordinates": coordinates,
            "tile_size_m": mat.get("blendermcp_tile_size_m") and list(mat["blendermcp_tile_size_m"]),
            "tile_source": tile_source,
            "mapping_scale": wired.pop("_mapping_scale"),
            "wired": {k: v for k, v in wired.items() if not k.startswith("_")},
            "note": note,
        }

    @command("assign_material")
    def assign_material(
        self, object_name, material, faces=None, normal=None, normal_tolerance_deg=30.0,
        min_z=None, max_z=None, slot=None, attribute=None,
    ):
        """Put a material on selected faces of a mesh, adding a slot if needed.

        Face rules combine with AND. With no rule every face gets it.
        """
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            raise ValueError(f"Object not found: {object_name}")
        if obj.type != "MESH":
            raise ValueError(f"{object_name} is a {obj.type}, not a mesh")
        if obj.mode == "EDIT":
            raise ValueError(f"{object_name} is in Edit Mode; switch to Object Mode first")
        mat = bpy.data.materials.get(material)
        if mat is None:
            raise ValueError(f"Material not found: {material}")

        selected = select_faces(
            obj, faces=faces, normal=normal, normal_tolerance_deg=normal_tolerance_deg,
            min_z=min_z, max_z=max_z, slot=slot, attribute=attribute,
        )
        mesh = obj.data
        if not selected:
            # Leave the mesh untouched rather than adding an unused slot.
            return {
                "object": obj.name,
                "material": mat.name,
                "faces_assigned": 0,
                "faces_total": len(mesh.polygons),
                "message": "No faces matched every rule; nothing changed. Rules combine "
                           "with AND; normals and heights are in world space.",
            }
        slot_added = False
        slot_index = next((i for i, m in enumerate(mesh.materials) if m == mat), None)
        if slot_index is None:
            # Faces index slots by position; a mesh with no slots renders with
            # the default material, which is slot 0 once one exists. Keep the
            # unselected faces looking the same by giving them a slot too.
            if len(mesh.materials) == 0 and len(selected) < len(mesh.polygons):
                mesh.materials.append(None)
            mesh.materials.append(mat)
            slot_index = len(mesh.materials) - 1
            slot_added = True
        for i in selected:
            mesh.polygons[i].material_index = slot_index
        mesh.update()

        per_slot = {}
        for p in mesh.polygons:
            m = mesh.materials[p.material_index] if p.material_index < len(mesh.materials) else None
            key = m.name if m else f"<empty slot {p.material_index}>"
            per_slot[key] = per_slot.get(key, 0) + 1
        return {
            "object": obj.name,
            "material": mat.name,
            "slot_index": slot_index,
            "slot_added": slot_added,
            "faces_assigned": len(selected),
            "faces_total": len(mesh.polygons),
            "faces_per_material": per_slot,
        }

    @command("set_world_hdri")
    def set_world_hdri(self, rotation_deg=None, strength=None, background_visible=None, background_color=None):
        """Rotate, brighten or hide (from camera) the current world HDRI."""
        world = bpy.context.scene.world
        if world is None:
            raise ValueError("The scene has no world. Import an HDRI first.")
        kwargs = {}
        if background_color is not None:
            kwargs["background_color"] = tuple(float(c) for c in background_color)
        return _apply_world_hdri(
            world, rotation_deg=rotation_deg, strength=strength,
            background_visible=background_visible, **kwargs,
        )

    @command("get_polyhaven_status")
    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = get_prefs().use_polyhaven
        if enabled:
            return {"enabled": True, "message": "PolyHaven integration is enabled and ready to use."}
        else:
            return {
                "enabled": False,
                "message": POLYHAVEN_DISABLED_HINT,
            }
