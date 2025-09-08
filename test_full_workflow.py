#!/usr/bin/env python3
"""
Test the complete BlenderMCP workflow from error to success
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from blender_mcp.server import get_scene_info, get_blender_connection
from unittest.mock import MagicMock

def test_workflow_stages():
    """Test different stages of the BlenderMCP workflow"""
    print("🧪 Testing Complete BlenderMCP Workflow")
    print("=" * 50)
    
    mock_ctx = MagicMock()
    
    # Stage 1: Try connection without server running
    print("📍 Stage 1: Testing connection without server running")
    print("-" * 30)
    
    try:
        # This should now provide intelligent error handling
        blender = get_blender_connection()
        print("✅ Connected successfully!")
        
        # Try to get scene info
        result = get_scene_info(mock_ctx)
        if len(result) > 100:  # If we got actual scene data
            print("✅ Scene info retrieved successfully!")
            print("📋 Scene data snippet:")
            print(result[:200] + "..." if len(result) > 200 else result)
        else:
            print("📋 Response from server:")
            print(result)
            
    except Exception as e:
        error_msg = str(e)
        print("📋 Intelligent Error Response:")
        print(error_msg[:500] + "..." if len(error_msg) > 500 else error_msg)
        
        # Check if it's our intelligent error
        if "BlenderMCP Setup Required" in error_msg or "Auto-Installation" in error_msg:
            print("✅ Intelligent error handling working!")
        else:
            print("❌ Basic error handling only")

    print("\n" + "=" * 50)
    print("🎯 Workflow Test Complete")
    
    print("\n📝 Next steps for complete testing:")
    print("1. Open Blender (should be starting)")
    print("2. Press 'N' in 3D viewport")
    print("3. Look for BlenderMCP tab")
    print("4. Click 'Connect to Claude'")
    print("5. Try this test again")

if __name__ == "__main__":
    test_workflow_stages()