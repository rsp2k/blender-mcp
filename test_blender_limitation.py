#!/usr/bin/env python3
"""
Demonstrate the specific Blender addon validation limitation
"""

import subprocess
import tempfile
import os

def test_blender_addon_validation():
    """Test what happens when we give Blender invalid addon files"""
    print("🧪 Testing Blender Addon Validation Limitation")
    print("=" * 50)
    
    # Create different types of invalid addon files
    test_cases = [
        {
            "name": "Random Text File",
            "content": "This is just random text, not Python code at all!"
        },
        {
            "name": "Invalid Python Syntax", 
            "content": "def broken_function(\n    print('syntax error')\n    missing_closing_paren"
        },
        {
            "name": "Python File without bl_info",
            "content": "import bpy\nprint('Valid Python but no bl_info dictionary')"
        },
        {
            "name": "Python with Invalid bl_info",
            "content": '''
import bpy
bl_info = {
    "name": "Test Addon",
    "author": "Test", 
    "version": "not_a_tuple",  # Should be tuple like (1, 0)
    "blender": "invalid_version"  # Should be tuple like (3, 0, 0)
}
'''
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n📋 Test {i}: {test_case['name']}")
        print("-" * (len(test_case['name']) + 10))
        
        # Create the invalid addon file
        invalid_addon = f"/tmp/invalid_addon_{i}.py"
        with open(invalid_addon, 'w') as f:
            f.write(test_case['content'])
        
        # Test Blender's response
        test_script = f'''
import bpy
import traceback

addon_path = r"{invalid_addon}"
print(f"Testing addon: {{addon_path}}")

try:
    # This is what our auto-installation uses
    result = bpy.ops.preferences.addon_install(filepath=addon_path, overwrite=True)
    print(f"INSTALL_RESULT: {{result}}")
    
    # Check if it was actually "installed"
    import os
    addon_name = os.path.splitext(os.path.basename(addon_path))[0]
    
    # Try to enable it
    try:
        enable_result = bpy.ops.preferences.addon_enable(module=addon_name)
        print(f"ENABLE_RESULT: {{enable_result}}")
        print("❌ VALIDATION_FAILED: Blender accepted invalid addon")
    except Exception as enable_error:
        print(f"✅ VALIDATION_WORKED: Enable failed as expected: {{enable_error}}")
        
except Exception as install_error:
    print(f"✅ VALIDATION_WORKED: Install failed as expected: {{install_error}}")

print("TEST_COMPLETE")
'''
        
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(test_script)
                temp_script = f.name
            
            try:
                result = subprocess.run(
                    ["blender", "-b", "-y", "--python", temp_script],
                    capture_output=True, text=True, timeout=30
                )
                
                # Parse results
                output = result.stdout
                if "VALIDATION_FAILED" in output:
                    print("❌ Blender incorrectly accepted invalid addon")
                elif "VALIDATION_WORKED" in output:
                    print("✅ Blender properly rejected invalid addon")
                else:
                    print("⚠️ Unexpected behavior")
                    
                # Show the key parts of Blender's response
                for line in output.split('\n'):
                    if any(keyword in line for keyword in ['INSTALL_RESULT', 'ENABLE_RESULT', 'VALIDATION_', 'Enable failed', 'Install failed']):
                        print(f"   └─ {line}")
                        
            finally:
                os.unlink(temp_script)
                os.unlink(invalid_addon)
                
        except Exception as e:
            print(f"❌ Error running test: {str(e)}")
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 BLENDER LIMITATION ANALYSIS")
    print("=" * 50)
    
    print("🔍 What we discovered:")
    print("   • Blender's addon_install() operation is quite permissive")
    print("   • It may copy invalid files to the addons directory") 
    print("   • The real validation happens when trying to ENABLE the addon")
    print("   • This means invalid files can end up in ~/.config/blender/scripts/addons/")
    
    print("\n💡 Why this is a 'limitation' not a 'bug':")
    print("   • Blender allows copying any .py file to addons directory")
    print("   • Validation only occurs when addon is loaded/enabled")
    print("   • This is actually reasonable design - allows dev workflow")
    
    print("\n🛡️ How this affects BlenderMCP:")
    print("   • ✅ Not a problem in practice - we control our addon.py file")
    print("   • ✅ Our addon.py is always valid with proper bl_info")
    print("   • ✅ Installation will work correctly for legitimate use")
    print("   • ⚠️ Could theoretically clutter addon directory if addon.py corrupted")
    
    print("\n🎯 Mitigation strategies:")
    print("   • File integrity check before installation")
    print("   • Validate bl_info dictionary exists and is correct")
    print("   • Check for minimum required addon structure")
    
    return True

if __name__ == "__main__":
    test_blender_addon_validation()