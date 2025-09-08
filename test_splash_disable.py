#!/usr/bin/env python3
"""
Test script to verify splash screen is disabled in Blender preferences
"""

import subprocess
import tempfile
import os

def test_splash_screen_setting():
    """Test if splash screen is properly disabled"""
    print("🧪 Testing Splash Screen Configuration")
    print("=" * 45)
    
    # Create a script to check splash screen setting
    check_script = '''
import bpy

# Check current splash screen setting
prefs = bpy.context.preferences
splash_enabled = prefs.view.show_splash

print(f"SPLASH_SCREEN_ENABLED: {splash_enabled}")

if splash_enabled:
    print("❌ Splash screen is ENABLED")
    print("💡 This means users will see the startup screen")
else:
    print("✅ Splash screen is DISABLED")
    print("🚀 Users will get straight to Blender interface")

# Also check other MCP-optimized settings
print(f"SAVE_PROMPT_DISABLED: {not prefs.view.use_save_prompt}")
print(f"MOUSE_EMULATION_ENABLED: {prefs.inputs.use_mouse_emulate_3_button}")
print(f"SAVE_VERSION_COUNT: {prefs.filepaths.save_version}")
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(check_script)
            temp_script = f.name
        
        try:
            print("🔍 Checking current Blender preferences...")
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            output = result.stdout
            print("\n📋 Blender Preferences Status:")
            print("-" * 30)
            
            for line in output.split('\n'):
                if 'SPLASH_SCREEN_ENABLED' in line:
                    enabled = 'True' in line
                    if enabled:
                        print("❌ Splash Screen: ENABLED (will show startup screen)")
                    else:
                        print("✅ Splash Screen: DISABLED (direct to interface)")
                        
                elif 'SAVE_PROMPT_DISABLED' in line:
                    disabled = 'True' in line
                    if disabled:
                        print("✅ Save Prompt: DISABLED (no exit prompts)")
                    else:
                        print("⚠️ Save Prompt: ENABLED (will prompt on exit)")
                        
                elif 'MOUSE_EMULATION_ENABLED' in line:
                    enabled = 'True' in line
                    if enabled:
                        print("✅ Mouse Emulation: ENABLED (3-button mouse support)")
                    else:
                        print("⚠️ Mouse Emulation: DISABLED")
                        
                elif line.startswith('✅') or line.startswith('❌') or line.startswith('💡') or line.startswith('🚀'):
                    print(f"  {line}")
                    
        finally:
            os.unlink(temp_script)
            
    except Exception as e:
        print(f"❌ Error checking preferences: {str(e)}")
        return False
    
    print("\n" + "=" * 45)
    return True

def apply_splash_disable_fix():
    """Apply splash screen disable if needed"""
    print("\n🔧 Applying Splash Screen Fix")
    print("=" * 35)
    
    fix_script = '''
import bpy

# Configure preferences for optimal MCP usage
prefs = bpy.context.preferences

# Disable splash screen
prefs.view.show_splash = False
print("✅ Splash screen disabled")

# Disable save prompts  
prefs.view.use_save_prompt = False
print("✅ Save prompts disabled")

# Enable mouse emulation for users without 3-button mouse
prefs.inputs.use_mouse_emulate_3_button = True
print("✅ Mouse emulation enabled")

# Set reasonable save version count
prefs.filepaths.save_version = 2
print("✅ Save versions set to 2")

# Save preferences
bpy.ops.wm.save_userpref()
print("✅ Preferences saved")

print("🎉 PREFERENCES_OPTIMIZED_SUCCESSFULLY")
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(fix_script)
            temp_script = f.name
        
        try:
            print("🔧 Applying optimal MCP preferences...")
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if "PREFERENCES_OPTIMIZED_SUCCESSFULLY" in result.stdout:
                print("✅ Preferences successfully optimized!")
                return True
            else:
                print("❌ Failed to optimize preferences")
                print("Output:", result.stdout)
                return False
                
        finally:
            os.unlink(temp_script)
            
    except Exception as e:
        print(f"❌ Error applying fix: {str(e)}")
        return False

if __name__ == "__main__":
    print("🎨 BlenderMCP Splash Screen Configuration Test")
    print("=" * 50)
    
    # Test current settings
    test_splash_screen_setting()
    
    # Ask user if they want to apply the fix
    print("\n" + "?" * 50)
    response = input("Apply splash screen fix? (y/n): ").lower().strip()
    
    if response in ['y', 'yes']:
        success = apply_splash_disable_fix()
        if success:
            print("\n🎉 Configuration complete!")
            print("💡 Next time you start Blender, you won't see the splash screen")
            
            # Test again to verify
            print("\n🔍 Verifying changes...")
            test_splash_screen_setting()
        else:
            print("\n❌ Configuration failed. Check the output above for details.")
    else:
        print("\n👍 No changes made. Preferences remain as they are.")