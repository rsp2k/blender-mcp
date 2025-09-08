#!/usr/bin/env python3
"""
Automated Blender MCP Addon Installation Script

This script automates the installation of the BlenderMCP addon in Blender.
It can be run via Blender's CLI to install and enable the addon without GUI interaction.

Usage:
    blender -b -y --python install_addon.py
    or
    blender -b -y --python install_addon.py -- --addon-path /path/to/addon.py
"""

import bpy
import os
import sys
import argparse
from pathlib import Path


def get_addon_info():
    """Extract addon info from the addon.py file"""
    addon_path = Path(__file__).parent / "addon.py"
    
    if not addon_path.exists():
        raise FileNotFoundError(f"Addon file not found at: {addon_path}")
    
    # Read the bl_info from addon.py
    with open(addon_path, 'r') as f:
        content = f.read()
    
    # Extract bl_info dictionary (simple approach)
    bl_info_start = content.find('bl_info = {')
    if bl_info_start == -1:
        raise ValueError("Could not find bl_info in addon.py")
    
    # Find the closing brace
    brace_count = 0
    bl_info_end = bl_info_start
    for i, char in enumerate(content[bl_info_start:], bl_info_start):
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                bl_info_end = i + 1
                break
    
    # Extract and evaluate bl_info
    bl_info_code = content[bl_info_start:bl_info_end]
    local_vars = {}
    exec(bl_info_code, {}, local_vars)
    
    return addon_path, local_vars['bl_info']


def install_blender_mcp_addon(addon_path=None):
    """
    Install and enable the BlenderMCP addon
    
    Args:
        addon_path: Optional path to addon.py file. If None, uses ./addon.py
    """
    
    if addon_path is None:
        addon_path, bl_info = get_addon_info()
    else:
        addon_path = Path(addon_path)
        if not addon_path.exists():
            raise FileNotFoundError(f"Addon file not found at: {addon_path}")
        
        # For external paths, we need to determine the module name
        # This is a simplified approach - in practice you'd parse the bl_info
        bl_info = {"name": "Blender MCP"}
    
    addon_path = addon_path.resolve()
    
    print(f"Installing BlenderMCP addon from: {addon_path}")
    print(f"Addon info: {bl_info.get('name', 'Unknown')} v{bl_info.get('version', 'Unknown')}")
    
    try:
        # Install the addon
        bpy.ops.preferences.addon_install(
            filepath=str(addon_path), 
            overwrite=True
        )
        print("✓ Addon installed successfully")
        
        # Enable the addon
        # The module name is typically the filename without extension
        module_name = addon_path.stem
        bpy.ops.preferences.addon_enable(module=module_name)
        print(f"✓ Addon '{module_name}' enabled successfully")
        
        # Configure preferences for optimal MCP usage
        prefs = bpy.context.preferences
        
        # Disable splash screen for smoother automation
        prefs.view.show_splash = False
        print("✓ Splash screen disabled")
        
        # Disable save prompts on exit for better automation
        prefs.view.use_save_prompt = False
        print("✓ Save prompts disabled")
        
        # Enable mouse emulation for users without 3-button mouse
        prefs.inputs.use_mouse_emulate_3_button = True
        print("✓ Mouse emulation enabled")
        
        # Set reasonable save version count
        prefs.filepaths.save_version = 2
        print("✓ Save versions optimized")
        
        # Save user preferences to persist the installation
        bpy.ops.wm.save_userpref()
        print("✓ User preferences saved")
        
        # Verify the addon is enabled
        if module_name in bpy.context.preferences.addons:
            print(f"✓ Addon '{module_name}' is now active")
            
            # Try to access the addon's functionality
            addon_prefs = bpy.context.preferences.addons.get(module_name)
            if addon_prefs:
                print(f"✓ Addon preferences accessible")
        else:
            print(f"⚠ Warning: Addon '{module_name}' not found in active addons")
        
        print("\n🎉 BlenderMCP addon installation completed successfully!")
        print("\nNext steps:")
        print("1. Open Blender normally (with GUI)")
        print("2. In the 3D Viewport, press 'N' to open the sidebar")
        print("3. Look for the 'BlenderMCP' tab")
        print("4. Click 'Connect to Claude' to start the server")
        
        return True
        
    except Exception as e:
        print(f"❌ Error installing addon: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main function to handle command line arguments and install addon"""
    
    # Parse command line arguments (after --)
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    
    parser = argparse.ArgumentParser(description="Install BlenderMCP addon")
    parser.add_argument(
        "--addon-path", 
        help="Path to addon.py file (default: ./addon.py)",
        default=None
    )
    
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse calls sys.exit() on error, but we want to handle it gracefully
        print("Using default addon path: ./addon.py")
        args = argparse.Namespace(addon_path=None)
    
    try:
        success = install_blender_mcp_addon(args.addon_path)
        if not success:
            print("❌ Addon installation failed")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Fatal error: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    print("🔧 BlenderMCP Addon Installation Script")
    print("=" * 50)
    main()