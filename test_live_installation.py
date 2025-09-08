#!/usr/bin/env python3
"""
Test what happens when we try to install BlenderMCP addon while Blender is running
"""

import subprocess
import tempfile
import os
import time
import signal
import psutil

def check_running_blender_processes():
    """Check if any Blender processes are currently running"""
    blender_processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and 'blender' in proc.info['name'].lower():
                blender_processes.append({
                    'pid': proc.info['pid'],
                    'name': proc.info['name'],
                    'cmdline': ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return blender_processes

def test_installation_while_running():
    """Test installing addon while Blender GUI is running"""
    print("🧪 Testing Addon Installation While Blender Is Running")
    print("=" * 60)
    
    # Step 1: Check current state
    print("📋 Step 1: Checking current Blender processes...")
    running_processes = check_running_blender_processes()
    
    if running_processes:
        print("✅ Found running Blender processes:")
        for proc in running_processes:
            print(f"  • PID {proc['pid']}: {proc['name']}")
            if '--python' in proc['cmdline']:
                print("    └─ Running with Python script")
    else:
        print("⚠️ No Blender processes currently running")
        print("💡 Starting Blender for this test...")
        
        # Start Blender in background
        blender_proc = subprocess.Popen(['blender'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)  # Give it time to start
        
        print(f"✅ Started Blender (PID: {blender_proc.pid})")
    
    # Step 2: Test installation script while Blender is running
    print("\n📋 Step 2: Testing installation while Blender is running...")
    
    install_script = '''
import bpy
import sys
import os

print("🔍 TESTING: Installation while Blender is running")

try:
    # Check if addon is already installed
    if "addon" in bpy.context.preferences.addons:
        print("✅ Addon is already enabled")
        addon_info = bpy.context.preferences.addons["addon"]
        print(f"   Module path: {addon_info.module}")
    else:
        print("❌ Addon not currently enabled")
    
    # Try to install/reinstall the addon
    addon_path = os.path.abspath("addon.py")
    if os.path.exists(addon_path):
        print(f"📁 Found addon file: {addon_path}")
        
        # Attempt installation
        bpy.ops.preferences.addon_install(filepath=addon_path, overwrite=True)
        print("✅ INSTALL_SUCCESS: Addon installation completed")
        
        # Try to enable it
        bpy.ops.preferences.addon_enable(module="addon")
        print("✅ ENABLE_SUCCESS: Addon enabled")
        
        # Check if it's actually working
        if "addon" in bpy.context.preferences.addons:
            print("✅ VERIFICATION_SUCCESS: Addon is active")
            
            # Test if the server can be started (this is the critical test)
            scene = bpy.context.scene
            if hasattr(scene, 'blendermcp_server_running'):
                print("✅ MCP_PROPERTIES_AVAILABLE: Server properties accessible")
            else:
                print("❌ MCP_PROPERTIES_MISSING: Server properties not found")
                
        else:
            print("❌ VERIFICATION_FAILED: Addon not active after enable")
        
        # Save preferences
        bpy.ops.wm.save_userpref()
        print("✅ PREFERENCES_SAVED: Changes persisted")
        
    else:
        print(f"❌ ADDON_NOT_FOUND: {addon_path}")
        
    print("🎯 LIVE_INSTALLATION_TEST_COMPLETE")
    
except Exception as e:
    print(f"❌ ERROR_DURING_INSTALLATION: {str(e)}")
    import traceback
    traceback.print_exc()
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(install_script)
            temp_script = f.name
        
        try:
            print("🔧 Running installation test script...")
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=45
            )
            
            print("\n📋 Installation Test Results:")
            print("-" * 40)
            
            # Parse and display results
            for line in result.stdout.split('\n'):
                if (line.startswith('✅') or line.startswith('❌') or 
                    'SUCCESS' in line or 'FAILED' in line or 'ERROR' in line):
                    print(line)
            
            # Check for specific outcomes
            if "LIVE_INSTALLATION_TEST_COMPLETE" in result.stdout:
                print("\n🎉 Test completed successfully!")
                
                if "VERIFICATION_SUCCESS" in result.stdout:
                    print("✅ Live installation appears to work!")
                    
                    if "MCP_PROPERTIES_AVAILABLE" in result.stdout:
                        print("✅ MCP server properties are accessible")
                        return "SUCCESS"
                    else:
                        print("⚠️ MCP properties may not be fully loaded")
                        return "PARTIAL"
                else:
                    print("❌ Live installation verification failed")
                    return "FAILED"
            else:
                print("❌ Test did not complete properly")
                return "ERROR"
                
        finally:
            os.unlink(temp_script)
            
    except subprocess.TimeoutExpired:
        print("⏰ Test timed out - this might indicate an issue with live installation")
        return "TIMEOUT"
    except Exception as e:
        print(f"❌ Error running test: {str(e)}")
        return "ERROR"

def test_addon_reload_behavior():
    """Test if Blender can reload addons without restart"""
    print("\n🔄 Testing Addon Reload Behavior")
    print("=" * 40)
    
    reload_script = '''
import bpy
import sys

print("🔍 Testing addon reload capabilities...")

try:
    # Check if we can disable and re-enable addon
    if "addon" in bpy.context.preferences.addons:
        print("✅ Addon currently enabled")
        
        # Try to disable
        bpy.ops.preferences.addon_disable(module="addon")
        print("✅ DISABLE_SUCCESS: Addon disabled")
        
        # Check if it's actually disabled
        if "addon" not in bpy.context.preferences.addons:
            print("✅ DISABLE_VERIFIED: Addon no longer active")
        else:
            print("❌ DISABLE_FAILED: Addon still active")
            
        # Try to re-enable
        bpy.ops.preferences.addon_enable(module="addon")
        print("✅ ENABLE_SUCCESS: Addon re-enabled")
        
        # Verify it's working again
        if "addon" in bpy.context.preferences.addons:
            print("✅ RELOAD_SUCCESS: Addon successfully reloaded")
        else:
            print("❌ RELOAD_FAILED: Addon not active after re-enable")
            
    else:
        print("❌ No addon to test reload with")
        
    print("🎯 RELOAD_TEST_COMPLETE")
    
except Exception as e:
    print(f"❌ ERROR_DURING_RELOAD: {str(e)}")
'''
    
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(reload_script)
            temp_script = f.name
        
        try:
            result = subprocess.run(
                ["blender", "-b", "-y", "--python", temp_script],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            print("📋 Reload Test Results:")
            for line in result.stdout.split('\n'):
                if (line.startswith('✅') or line.startswith('❌') or 
                    'SUCCESS' in line or 'FAILED' in line):
                    print(line)
                    
        finally:
            os.unlink(temp_script)
            
    except Exception as e:
        print(f"❌ Error in reload test: {str(e)}")

if __name__ == "__main__":
    print("🔬 BlenderMCP Live Installation Analysis")
    print("=" * 50)
    
    # Test live installation
    result = test_installation_while_running()
    
    # Test reload behavior
    test_addon_reload_behavior()
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 ANALYSIS SUMMARY")
    print("-" * 25)
    
    if result == "SUCCESS":
        print("✅ Live installation works perfectly!")
        print("💡 Blender can install and activate addons without restart")
    elif result == "PARTIAL":
        print("⚠️ Live installation works but may need addon restart")
        print("💡 Consider adding addon reload logic to MCP server")
    elif result == "FAILED":
        print("❌ Live installation has issues")
        print("💡 May need to handle running Blender differently")
    elif result == "TIMEOUT":
        print("⏰ Live installation may cause hangs")
        print("💡 Consider checking for running processes first")
    else:
        print("❓ Unable to determine live installation behavior")
        print("💡 More investigation needed")
        
    print("\n🔍 Next steps based on results:")
    print("1. Implement running process detection in MCP server")
    print("2. Add appropriate handling for live vs fresh installations")
    print("3. Consider addon reload mechanisms if needed")