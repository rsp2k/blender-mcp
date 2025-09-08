#!/usr/bin/env python3
"""
Apply splash screen disable fix for BlenderMCP
"""

import subprocess
import tempfile
import os

def apply_splash_disable_fix():
    """Apply splash screen disable and other MCP optimizations"""
    print("🔧 Applying BlenderMCP Preferences Optimization")
    print("=" * 50)
    
    fix_script = '''
import bpy

try:
    # Configure preferences for optimal MCP usage
    prefs = bpy.context.preferences
    
    # Disable splash screen - the main fix!
    prefs.view.show_splash = False
    print("✅ Splash screen disabled")
    
    # Disable save prompts on exit
    prefs.view.use_save_prompt = False
    print("✅ Save prompts disabled")
    
    # Enable mouse emulation for users without 3-button mouse
    prefs.inputs.use_mouse_emulate_3_button = True
    print("✅ Mouse emulation enabled")
    
    # Set reasonable save version count
    prefs.filepaths.save_version = 2
    print("✅ Save versions set to 2")
    
    # Save preferences to make changes persistent
    bpy.ops.wm.save_userpref()
    print("✅ Preferences saved to disk")
    
    print("🎉 BLENDERMCP_PREFERENCES_OPTIMIZED")
    
except Exception as e:
    print(f"❌ ERROR: {str(e)}")
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(fix_script)
            temp_script = f.name
        
        try:
            print("🔧 Running Blender to apply preferences...")
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            print("\n📋 Results:")
            print("-" * 20)
            
            # Show the relevant output
            for line in result.stdout.split('\n'):
                if line.startswith('✅') or line.startswith('❌') or 'BLENDERMCP_PREFERENCES_OPTIMIZED' in line:
                    print(line)
            
            if "BLENDERMCP_PREFERENCES_OPTIMIZED" in result.stdout:
                print("\n🎉 SUCCESS: Splash screen and preferences optimized!")
                return True
            else:
                print("\n❌ FAILED: Could not optimize preferences")
                print("Full output:", result.stdout)
                return False
                
        finally:
            os.unlink(temp_script)
            
    except Exception as e:
        print(f"❌ Error applying preferences: {str(e)}")
        return False

def verify_splash_fix():
    """Verify the splash screen is now disabled"""
    print("\n🔍 Verifying Splash Screen Fix")
    print("=" * 35)
    
    check_script = '''
import bpy

prefs = bpy.context.preferences
splash_enabled = prefs.view.show_splash

if splash_enabled:
    print("❌ VERIFICATION_FAILED: Splash screen still enabled")
else:
    print("✅ VERIFICATION_SUCCESS: Splash screen disabled")
    
# Check other settings too
print(f"Save Prompt: {'Disabled' if not prefs.view.use_save_prompt else 'Enabled'}")
print(f"Mouse Emulation: {'Enabled' if prefs.inputs.use_mouse_emulate_3_button else 'Disabled'}")
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(check_script)
            temp_script = f.name
        
        try:
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            for line in result.stdout.split('\n'):
                if 'VERIFICATION' in line or line.startswith('Save Prompt') or line.startswith('Mouse Emulation'):
                    print(line)
            
            return "VERIFICATION_SUCCESS" in result.stdout
            
        finally:
            os.unlink(temp_script)
            
    except Exception as e:
        print(f"❌ Error verifying: {str(e)}")
        return False

if __name__ == "__main__":
    print("🎨 BlenderMCP Splash Screen Fix")
    print("=" * 35)
    
    # Apply the fix
    success = apply_splash_disable_fix()
    
    if success:
        # Verify it worked
        verified = verify_splash_fix()
        
        if verified:
            print("\n🎉 COMPLETE: Splash screen successfully disabled!")
            print("💡 Blender will now start directly to the interface")
            print("🚀 Perfect for automated MCP usage!")
        else:
            print("\n⚠️ Applied but verification failed")
    else:
        print("\n❌ Failed to apply splash screen fix")
        print("💡 You may need to disable it manually in Blender preferences")