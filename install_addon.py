#!/usr/bin/env python3
"""Automated BlenderMCP addon installation, executed inside Blender.

Usage::

    blender -b -y --python install_addon.py
    # or to point at a specific addon source:
    blender -b -y --python install_addon.py -- --addon-path /path/to/addon

Two install paths are supported, in priority order:

1. **Package directory** (default since the 9-phase refactor). If the
   project ships an ``addon/`` directory next to this script, it's
   zipped on the fly and handed to
   ``bpy.ops.preferences.addon_install(filepath=<zip>)``. Blender
   unpacks the zip into its ``scripts/addons/`` tree so the package
   layout is preserved.

2. **Single-file shim** (fallback). If only ``addon.py`` is present,
   it's installed as-is. Useful for the ad-hoc "Install Add-on" file
   dialog flow inside the Blender UI; less useful programmatically
   because the shim's ``from addon import register, unregister``
   needs the ``addon/`` package alongside it.
"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import bpy


PROJECT_ROOT = Path(__file__).resolve().parent


def _make_addon_zip(pkg_dir: Path, dest: Path) -> Path:
    """Zip ``pkg_dir`` so it's installable via ``addon_install``.

    Entries are stored under the package name as the top-level directory
    so the resulting tree at ``scripts/addons/<pkg>/...`` is correct.
    ``__pycache__`` and ``*.pyc`` are skipped to keep the archive small.
    """
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(pkg_dir):
            # Don't descend into bytecode caches.
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".pyc"):
                    continue
                abs_path = Path(root) / name
                # Store with `<pkg>/` prefix so Blender unpacks into a
                # package directory of the same name.
                arcname = pkg_dir.name + "/" + str(abs_path.relative_to(pkg_dir))
                zf.write(abs_path, arcname)
    return dest


def _resolve_addon_source(override: str | None) -> tuple[Path, str]:
    """Return (filepath_to_install, module_name_to_enable).

    For a package directory, returns the zip path and the package name
    (e.g. ``"addon"``). For a single-file shim, returns the .py path
    and the filename stem.
    """
    if override:
        explicit = Path(override).resolve()
        if explicit.is_dir() and (explicit / "__init__.py").exists():
            zip_path = Path(tempfile.mkstemp(suffix=".zip", prefix="addon-")[1])
            _make_addon_zip(explicit, zip_path)
            return zip_path, explicit.name
        if explicit.is_file() and explicit.suffix == ".py":
            return explicit, explicit.stem
        raise FileNotFoundError(f"Addon source not found at: {override}")

    # Default discovery: prefer addon/ package, fall back to addon.py shim.
    pkg_dir = PROJECT_ROOT / "addon"
    if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
        zip_path = Path(tempfile.mkstemp(suffix=".zip", prefix="addon-")[1])
        _make_addon_zip(pkg_dir, zip_path)
        return zip_path, pkg_dir.name

    shim = PROJECT_ROOT / "addon.py"
    if shim.is_file():
        return shim, shim.stem

    raise FileNotFoundError(
        f"Neither addon/ nor addon.py found alongside {__file__}"
    )


def install_blender_mcp_addon(addon_path: str | None = None) -> bool:
    """Install + enable the addon. Returns True on success."""

    try:
        filepath, module_name = _resolve_addon_source(addon_path)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return False

    print(f"Installing BlenderMCP from: {filepath}")
    print(f"Module name to enable:     {module_name}")

    try:
        bpy.ops.preferences.addon_install(filepath=str(filepath), overwrite=True)
        print("✓ Addon installed successfully")

        bpy.ops.preferences.addon_enable(module=module_name)
        print(f"✓ Addon '{module_name}' enabled successfully")

        # Configure preferences for optimal MCP usage (unchanged from pre-refactor).
        prefs = bpy.context.preferences
        prefs.view.show_splash = False
        print("✓ Splash screen disabled")
        prefs.view.use_save_prompt = False
        print("✓ Save prompts disabled")
        prefs.inputs.use_mouse_emulate_3_button = True
        print("✓ Mouse emulation enabled")
        prefs.filepaths.save_version = 2
        print("✓ Save versions optimized")

        bpy.ops.wm.save_userpref()
        print("✓ User preferences saved")

        if module_name in bpy.context.preferences.addons:
            print(f"✓ Addon '{module_name}' is now active")
        else:
            print(f"⚠ Warning: Addon '{module_name}' not found in active addons")

        print("\n🎉 BlenderMCP addon installation completed successfully!")
        print("\nNext steps:")
        print("1. Open Blender normally (with GUI)")
        print("2. In the 3D Viewport, press 'N' to open the sidebar")
        print("3. Look for the 'BlenderMCP' tab")
        print("4. Enter your username/password and click Login")
        print("5. Click 'Connect' to start the bus client")
        return True

    except Exception as e:
        print(f"❌ Error installing addon: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # If we created a temp zip, clean it up.
        if str(filepath).endswith(".zip") and "addon-" in filepath.name:
            try:
                os.unlink(filepath)
            except OSError:
                pass


def main():
    # Parse args after the `--` separator (Blender's convention).
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    parser = argparse.ArgumentParser(description="Install BlenderMCP addon")
    parser.add_argument(
        "--addon-path",
        help="Path to addon/ package or addon.py file. Defaults to auto-discovery alongside this script.",
        default=None,
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("Using default addon source (auto-discovery alongside install_addon.py)")
        args = argparse.Namespace(addon_path=None)

    if not install_blender_mcp_addon(args.addon_path):
        sys.exit(1)


if __name__ == "__main__":
    print("🔧 BlenderMCP Addon Installation Script")
    print("=" * 50)
    main()
