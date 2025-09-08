"""
Tests for BlenderMCP connection scenarios and error handling
"""
import pytest
import asyncio
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the src directory to the path so we can import the server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from blender_mcp.server import mcp, get_blender_connection, BlenderConnection


class TestBlenderMCPConnectionScenarios:
    """Test various connection scenarios for BlenderMCP server"""

    def test_tool_function_without_blender_connection(self):
        """Test what happens when tools are called without Blender connection"""
        # Import the tool function directly
        from blender_mcp.server import get_scene_info
        
        # Mock the global connection to fail
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_get_conn.side_effect = Exception("Could not connect to Blender. Make sure the Blender addon is running.")
            
            # Create a mock context
            mock_ctx = MagicMock()
            
            # Try to call get_scene_info tool function
            result = get_scene_info(mock_ctx)
            
            # Should return an error message, not crash
            assert "Error getting scene info" in result
            assert "Could not connect to Blender" in result

    async def test_tool_call_with_connection_timeout(self, client):
        """Test tool behavior when Blender connection times out"""
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_connection = Mock()
            mock_connection.send_command.side_effect = Exception("Timeout waiting for Blender response - try simplifying your request")
            mock_get_conn.return_value = mock_connection
            
            result = await client.call_tool("get_scene_info", {})
            
            assert "Error getting scene info" in result
            assert "Timeout waiting for Blender response" in result

    async def test_tool_call_with_socket_error(self, client):
        """Test tool behavior when socket connection is lost"""
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_connection = Mock()
            mock_connection.send_command.side_effect = Exception("Connection to Blender lost: [Errno 32] Broken pipe")
            mock_get_conn.return_value = mock_connection
            
            result = await client.call_tool("get_object_info", {"object_name": "Cube"})
            
            assert "Error getting object info" in result
            assert "Connection to Blender lost" in result

    async def test_viewport_screenshot_without_connection(self, client):
        """Test screenshot tool without connection"""
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_get_conn.side_effect = Exception("Could not connect to Blender")
            
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("get_viewport_screenshot", {"max_size": 800})
            
            assert "Could not connect to Blender" in str(exc_info.value)

    async def test_execute_code_without_connection(self, client):
        """Test code execution without connection"""
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_get_conn.side_effect = Exception("Could not connect to Blender")
            
            result = await client.call_tool("execute_blender_code", {"code": "print('hello')"})
            
            assert "Error executing code" in result
            assert "Could not connect to Blender" in result

    async def test_polyhaven_status_without_connection(self, client):
        """Test PolyHaven status check without connection"""
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_get_conn.side_effect = Exception("Could not connect to Blender")
            
            result = await client.call_tool("get_polyhaven_status", {})
            
            assert "Error checking PolyHaven status" in result
            assert "Could not connect to Blender" in result

    def test_blender_connection_creation(self):
        """Test BlenderConnection class behavior"""
        conn = BlenderConnection(host="localhost", port=9876)
        
        # Test connection attempt to non-existent server
        success = conn.connect()
        assert success is False
        assert conn.sock is None

    @pytest.mark.integration
    async def test_connection_retry_logic(self, client):
        """Test connection retry behavior (integration test)"""
        with patch('blender_mcp.server._blender_connection', None):
            with patch('blender_mcp.server.BlenderConnection') as mock_conn_class:
                mock_instance = Mock()
                mock_instance.connect.return_value = False
                mock_conn_class.return_value = mock_instance
                
                # Should raise exception when connection fails
                with pytest.raises(Exception) as exc_info:
                    get_blender_connection()
                
                assert "Could not connect to Blender" in str(exc_info.value)

    async def test_all_tools_handle_connection_errors_gracefully(self, client):
        """Test that all tools handle connection errors without crashing the server"""
        tools_to_test = [
            ("get_scene_info", {}),
            ("get_object_info", {"object_name": "Cube"}),
            ("execute_blender_code", {"code": "print('test')"}),
            ("get_polyhaven_status", {}),
            ("get_hyper3d_status", {}),
            ("get_sketchfab_status", {}),
        ]
        
        with patch('blender_mcp.server.get_blender_connection') as mock_get_conn:
            mock_get_conn.side_effect = Exception("Connection failed")
            
            for tool_name, params in tools_to_test:
                result = await client.call_tool(tool_name, params)
                # Should return error message, not crash
                assert isinstance(result, str)
                assert "Error" in result or "Connection failed" in result


if __name__ == "__main__":
    # Run tests manually if needed
    pytest.main([__file__, "-v"])