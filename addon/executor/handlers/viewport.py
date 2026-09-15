"""Viewport screenshot handler."""

from __future__ import annotations

import bpy

from ..registry import command

# Blender's file_format enum uses its own spellings, so the obvious
# format.upper() is wrong for the common cases: "jpg" -> "JPG" is not a
# member and the assignment raises. Anything unmapped falls through
# upper-cased, which covers PNG/BMP/AVIF/WEBP as-is.
_FILE_FORMAT_ALIASES = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "tif": "TIFF",
    "tiff": "TIFF",
    "tga": "TARGA",
    "exr": "OPEN_EXR",
}


def _blender_file_format(fmt: str) -> str:
    return _FILE_FORMAT_ALIASES.get(fmt.lower(), fmt.upper())


class ViewportHandlersMixin:
    """`get_viewport_screenshot` command."""

    @command("get_viewport_screenshot")
    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Render the current 3D viewport to an image file.

        Uses ``bpy.ops.render.opengl(view_context=True)`` so the output is the
        viewport's own pixels drawn by the render engine into Blender's GPU
        framebuffer, not a desktop screengrab. Unaffected by window occlusion,
        virtual-desktop position, minimize state, or the OS compositor. Fails
        cleanly in ``--background`` mode where no viewport exists.

        The prior implementation called ``screen.screenshot_area``, which
        reads back from the OS window rectangle and returns whatever pixels
        the compositor is drawing there (i.e. any window on top of Blender).

        Parameters:
        - max_size: Maximum size in pixels for the largest dimension
        - filepath: Path to write the image to
        - format: Image format (png, jpg, etc.)
        """
        if not filepath:
            return {"error": "No filepath provided"}
        if bpy.app.background:
            return {"error": "No viewport available in --background mode"}

        area = next(
            (a for a in bpy.context.screen.areas if a.type == "VIEW_3D"), None
        )
        if not area:
            return {"error": "No 3D viewport in the active screen"}

        # render.opengl needs a WINDOW-typed region so it can pick up the
        # right view matrix. Areas also carry HEADER/TOOLS/UI regions.
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        if not region:
            return {"error": "3D viewport has no WINDOW region"}

        scene = bpy.context.scene
        r = scene.render
        # Snapshot settings so a screenshot capture never leaks into the
        # user's next real render.
        saved = (
            r.resolution_x,
            r.resolution_y,
            r.resolution_percentage,
            r.image_settings.file_format,
        )
        try:
            r.resolution_x = region.width
            r.resolution_y = region.height
            r.resolution_percentage = 100
            r.image_settings.file_format = _blender_file_format(format)

            with bpy.context.temp_override(area=area, region=region):
                # view_context=True renders through the viewport's current
                # view matrix (persp / ortho / camera as displayed) instead
                # of the scene's active camera.
                bpy.ops.render.opengl(view_context=True)

            render_result = bpy.data.images.get("Render Result")
            if render_result is None:
                return {"error": "render.opengl produced no Render Result"}
            render_result.save_render(filepath=filepath)
        except Exception as e:
            return {"error": str(e)}
        finally:
            (
                r.resolution_x,
                r.resolution_y,
                r.resolution_percentage,
                r.image_settings.file_format,
            ) = saved

        # Downscale on disk if the capture exceeded max_size. Kept as a
        # post-pass instead of pre-shrinking the render resolution so the
        # written file's aspect matches the viewport region exactly.
        # Every exit from here returns a dict; the dispatch layer reports
        # {"error": ...} far better than it reports a raised traceback.
        try:
            img = bpy.data.images.load(filepath)
        except Exception as e:
            return {"error": f"capture written but not re-loadable: {e}"}
        try:
            width, height = img.size
            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)
                img.file_format = _blender_file_format(format)
                img.save()
                width, height = new_width, new_height
        except Exception as e:
            return {"error": f"downscale failed: {e}"}
        finally:
            bpy.data.images.remove(img)

        return {
            "success": True,
            "width": width,
            "height": height,
            "filepath": filepath,
        }
