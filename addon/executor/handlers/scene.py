"""Scene / object / data-browser handlers."""

from __future__ import annotations

import traceback

import bpy

from ..registry import command


def scene_named(name=None):
    """The scene called ``name``, or the active scene when it's empty."""
    if name is None or name == "":
        return bpy.context.scene
    found = bpy.data.scenes.get(str(name))
    if found is None:
        names = [s.name for s in bpy.data.scenes]
        raise ValueError(f"no scene named {name!r}; scenes: {names}")
    return found


class SceneHandlersMixin:
    """`get_scene_info`, `get_object_info`, `browse_data` commands.

    `get_object_info` calls `self._get_aabb` from SharedHelpersMixin;
    `browse_data` calls `self._get_data_item_info` from the same — both
    arrive via MRO once mixed into BlenderCommandExecutor.
    """

    @command("get_scene_info")
    def get_scene_info(self, scene=None):
        """Summary of the active scene, or of the scene named ``scene`` without
        switching to it."""
        try:
            target = scene_named(scene)
            active_scene = bpy.context.scene
            objects = list(target.objects)
            # Every scene in the file first, so a summary cut short for a
            # prompt still names them all.
            scene_info = {
                "name": target.name,
                "active_scene": active_scene.name,
                "scenes": [
                    {"name": s.name, "active": s == active_scene, "objects": len(s.objects)}
                    for s in bpy.data.scenes
                ],
                "object_count": len(objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }
            for obj in objects[:10]:
                scene_info["objects"].append({
                    "name": obj.name,
                    "type": obj.type,
                    "location": [round(float(obj.location.x), 2),
                                 round(float(obj.location.y), 2),
                                 round(float(obj.location.z), 2)],
                })

            # What the user has picked in the GUI, so "this one" needs no
            # execute_code round trip. Capped like the object list.
            if target == active_scene:
                view_layer = bpy.context.view_layer
            else:
                view_layer = target.view_layers[0] if len(target.view_layers) else None
            if view_layer is not None:
                selected = [o.name for o in view_layer.objects if o.select_get()]
                active = view_layer.objects.active
            else:
                selected, active = [], None
            scene_info["active_object"] = active.name if active else None
            scene_info["selected_objects"] = selected[:50]
            scene_info["selected_count"] = len(selected)

            type_counts = {}
            for o in objects:
                type_counts[o.type] = type_counts.get(o.type, 0) + 1
            scene_info["total_objects"] = len(objects)
            scene_info["type_counts"] = dict(sorted(type_counts.items()))
            if len(objects) > len(scene_info["objects"]):
                scene_info["objects_note"] = (
                    f"first {len(scene_info['objects'])} of {len(objects)} objects; "
                    "list_scene_objects lists them all with filters"
                )
            if len(bpy.data.scenes) > 1:
                scene_info["scenes_note"] = (
                    "pass scene=<name> to get_scene_info or list_scene_objects to look "
                    "inside another scene without switching to it"
                )
            return scene_info
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            print(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @command("get_object_info")
    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    @command("browse_data")
    def browse_data(self, collection=None, item_name=None, page=1, page_size=50, detail_level="summary"):
        """Browse bpy.data collections with pagination and detail levels

        Args:
            collection: Data collection to browse (e.g., "objects", "materials", "scenes")
            item_name: Specific item name to get details for
            page: Page number for pagination
            page_size: Items per page
            detail_level: Level of detail ("summary", "detailed", "full")
        """
        try:
            # Map of available data collections
            data_collections = {
                "actions": bpy.data.actions,
                "armatures": bpy.data.armatures,
                "brushes": bpy.data.brushes,
                "cache_files": bpy.data.cache_files,
                "cameras": bpy.data.cameras,
                "collections": bpy.data.collections,
                "curves": bpy.data.curves,
                "fonts": bpy.data.fonts,
                "grease_pencils": bpy.data.grease_pencils,
                "hair_curves": bpy.data.hair_curves,
                "images": bpy.data.images,
                "lattices": bpy.data.lattices,
                "libraries": bpy.data.libraries,
                "lightprobes": bpy.data.lightprobes,
                "lights": bpy.data.lights,
                "linestyles": bpy.data.linestyles,
                "masks": bpy.data.masks,
                "materials": bpy.data.materials,
                "meshes": bpy.data.meshes,
                "metaballs": bpy.data.metaballs,
                "movieclips": bpy.data.movieclips,
                "node_groups": bpy.data.node_groups,
                "objects": bpy.data.objects,
                "paint_curves": bpy.data.paint_curves,
                "palettes": bpy.data.palettes,
                "particles": bpy.data.particles,
                "pointclouds": bpy.data.pointclouds,
                "scenes": bpy.data.scenes,
                "screens": bpy.data.screens,
                "shape_keys": bpy.data.shape_keys,
                "sounds": bpy.data.sounds,
                "speakers": bpy.data.speakers,
                "texts": bpy.data.texts,
                "textures": bpy.data.textures,
                "volumes": bpy.data.volumes,
                "window_managers": bpy.data.window_managers,
                "workspaces": bpy.data.workspaces,
                "worlds": bpy.data.worlds,
            }

            # If no collection specified, list available collections
            if not collection:
                collections_info = []
                for name, coll in data_collections.items():
                    try:
                        count = len(coll)
                        collections_info.append({
                            "name": name,
                            "count": count,
                            "type": str(type(coll).__name__)
                        })
                    except Exception:
                        pass

                return {
                    "success": True,
                    "collections": collections_info,
                    "total": len(collections_info)
                }

            # Check if collection exists
            if collection not in data_collections:
                return {
                    "error": f"Unknown collection: {collection}",
                    "available": list(data_collections.keys())
                }

            data_collection = data_collections[collection]

            # If specific item requested
            if item_name:
                if item_name in data_collection:
                    item = data_collection[item_name]
                    item_info = self._get_data_item_info(item, collection, detail_level)
                    return {
                        "success": True,
                        "item": item_info,
                        "collection": collection
                    }
                else:
                    return {
                        "error": f"Item '{item_name}' not found in {collection}",
                        "available_count": len(data_collection)
                    }

            # Browse collection with pagination
            items = list(data_collection)
            total_items = len(items)
            total_pages = (total_items + page_size - 1) // page_size

            # Calculate pagination
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_items)
            page_items = items[start_idx:end_idx]

            # Get info for each item
            items_info = []
            for item in page_items:
                try:
                    item_info = self._get_data_item_info(item, collection, "summary")
                    items_info.append(item_info)
                except Exception as e:
                    items_info.append({
                        "name": getattr(item, "name", "unknown"),
                        "error": str(e)
                    })

            return {
                "success": True,
                "collection": collection,
                "items": items_info,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_items": total_items
            }

        except Exception as e:
            return {"error": f"Failed to browse data: {str(e)}"}
