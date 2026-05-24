"""Shared helpers used by multiple handler domains.

Mixed into BlenderCommandExecutor so siblings can keep calling
``self._get_aabb(...)`` etc. without knowing where the implementation
lives.

- ``_get_aabb``: world-space AABB of a mesh object. Called by
  ``get_object_info`` (scene domain) and ``import_generated_asset_*``
  (hyper3d domain).
- ``_clean_imported_glb``: tidy a freshly imported GLB to a single mesh
  (drops the empty-parent wrapper Blender adds). Called by the
  ``import_generated_asset_*`` variants.
- ``_get_data_item_info``: per-collection metadata for ``browse_data``.
  Long, mostly switch-like; only ``browse_data`` calls it.
"""

from __future__ import annotations

import bpy
import mathutils


class SharedHelpersMixin:
    """Shared bpy helpers attached to BlenderCommandExecutor."""

    @staticmethod
    def _get_aabb(obj):
        """Returns the world-space axis-aligned bounding box (AABB) of an object."""
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [
            [*min_corner], [*max_corner]
        ]

    @staticmethod
    def _clean_imported_glb(filepath, mesh_name=None):
        # Get the set of existing objects before import
        existing_objects = set(bpy.data.objects)

        # Import the GLB file
        bpy.ops.import_scene.gltf(filepath=filepath)

        # Ensure the context is updated
        bpy.context.view_layer.update()

        # Get all imported objects
        imported_objects = list(set(bpy.data.objects) - existing_objects)

        if not imported_objects:
            print("Error: No objects were imported.")
            return

        # Identify the mesh object
        mesh_obj = None

        if len(imported_objects) == 1 and imported_objects[0].type == 'MESH':
            mesh_obj = imported_objects[0]
            print("Single mesh imported, no cleanup needed.")
        else:
            if len(imported_objects) == 2:
                empty_objs = [i for i in imported_objects if i.type == "EMPTY"]
                if len(empty_objs) != 1:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
                parent_obj = empty_objs.pop()
                if len(parent_obj.children) == 1:
                    potential_mesh = parent_obj.children[0]
                    if potential_mesh.type == 'MESH':
                        print("GLB structure confirmed: Empty node with one mesh child.")

                        # Unparent the mesh from the empty node
                        potential_mesh.parent = None

                        # Remove the empty node
                        bpy.data.objects.remove(parent_obj)
                        print("Removed empty node, keeping only the mesh.")

                        mesh_obj = potential_mesh
                    else:
                        print("Error: Child is not a mesh object.")
                        return
                else:
                    print("Error: Expected an empty node with one mesh child or a single mesh object.")
                    return
            else:
                print("Error: Expected an empty node with one mesh child or a single mesh object.")
                return

        # Rename the mesh if needed
        try:
            if mesh_obj and mesh_obj.name is not None and mesh_name:
                mesh_obj.name = mesh_name
                if mesh_obj.data.name is not None:
                    mesh_obj.data.name = mesh_name
                print(f"Mesh renamed to: {mesh_name}")
        except Exception:
            print("Having issue with renaming, give up renaming.")

        return mesh_obj

    def _get_data_item_info(self, item, collection_type, detail_level="summary"):
        """Get information about a data item (used by browse_data)."""
        info = {
            "name": getattr(item, "name", "unnamed"),
            "type": type(item).__name__,
            "collection": collection_type
        }

        # Add common properties
        if hasattr(item, "users"):
            info["users"] = item.users
        if hasattr(item, "use_fake_user"):
            info["use_fake_user"] = item.use_fake_user
        if hasattr(item, "library"):
            info["library"] = item.library.filepath if item.library else None

        # Collection-specific info
        if collection_type == "objects":
            info["type_specific"] = item.type
            if detail_level != "summary":
                info["location"] = list(item.location)
                info["rotation"] = list(item.rotation_euler)
                info["scale"] = list(item.scale)
                info["visible"] = item.visible_get()
                if item.data:
                    info["data_name"] = item.data.name
                    info["data_type"] = type(item.data).__name__

        elif collection_type == "materials":
            info["node_tree"] = item.node_tree is not None
            if detail_level != "summary":
                info["use_nodes"] = item.use_nodes
                if item.node_tree and detail_level == "full":
                    info["nodes_count"] = len(item.node_tree.nodes)

        elif collection_type == "meshes":
            info["vertices"] = len(item.vertices)
            info["edges"] = len(item.edges)
            info["faces"] = len(item.polygons)
            if detail_level != "summary":
                info["has_custom_normals"] = item.has_custom_normals
                info["materials_count"] = len(item.materials)

        elif collection_type == "scenes":
            info["frame_start"] = item.frame_start
            info["frame_end"] = item.frame_end
            info["frame_current"] = item.frame_current
            if detail_level != "summary":
                info["render_engine"] = item.render.engine
                info["camera"] = item.camera.name if item.camera else None
                info["world"] = item.world.name if item.world else None

        elif collection_type == "images":
            info["size"] = list(item.size)
            info["filepath"] = item.filepath
            if detail_level != "summary":
                info["source"] = item.source
                info["packed"] = item.packed_file is not None
                info["has_data"] = item.has_data

        elif collection_type == "collections":
            info["objects_count"] = len(item.objects)
            info["children_count"] = len(item.children)
            if detail_level != "summary":
                info["hide_viewport"] = item.hide_viewport
                info["hide_render"] = item.hide_render

        elif collection_type == "node_groups":
            info["type"] = item.type
            if detail_level != "summary" and item.nodes:
                info["nodes_count"] = len(item.nodes)
                info["links_count"] = len(item.links)

        elif collection_type == "texts":
            info["filepath"] = item.filepath
            info["is_dirty"] = item.is_dirty
            info["is_in_memory"] = item.is_in_memory
            if detail_level == "full":
                info["lines_count"] = len(item.lines)
                if detail_level == "full" and len(item.lines) < 100:
                    info["content_preview"] = "\n".join([line.body for line in item.lines[:10]])

        elif collection_type == "actions":
            info["frame_range"] = list(item.frame_range)
            if detail_level != "summary":
                info["fcurves_count"] = len(item.fcurves)
                info["groups_count"] = len(item.groups)

        elif collection_type == "worlds":
            info["use_nodes"] = item.use_nodes
            if detail_level != "summary" and item.node_tree:
                info["nodes_count"] = len(item.node_tree.nodes)

        return info
