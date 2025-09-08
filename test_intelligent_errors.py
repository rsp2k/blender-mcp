#!/usr/bin/env python3
"""
Test script to demonstrate intelligent error handling in BlenderMCP
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from blender_mcp.server import get_scene_info
from unittest.mock import MagicMock

def test_intelligent_error_response():
    """Test the intelligent error response system"""
    print("🧪 Testing BlenderMCP Intelligent Error System")
    print("=" * 50)
    
    # Create mock context
    mock_ctx = MagicMock()
    
    # Call tool without Blender connection (should trigger intelligent error)
    print("Calling get_scene_info() without Blender connection...")
    print()
    
    try:
        result = get_scene_info(mock_ctx)
        print("📋 Error Response:")
        print(result)
    except Exception as e:
        print(f"❌ Unexpected exception: {str(e)}")

if __name__ == "__main__":
    test_intelligent_error_response()