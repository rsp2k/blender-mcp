# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BlenderMCP is a Model Context Protocol (MCP) server that enables Claude AI to directly interact with and control Blender 3D through a socket-based communication system. The project consists of two main components:

1. **Blender Addon** (`addon.py`): A Blender addon that creates a socket server within Blender to receive and execute commands
2. **MCP Server** (`src/blender_mcp/server.py`): A Python server that implements the Model Context Protocol and connects to the Blender addon

## Architecture

### Communication Flow
- MCP Server (Claude) ↔ Socket Connection ↔ Blender Addon ↔ Blender Python API
- Uses JSON-based protocol over TCP sockets (default: localhost:9876)
- Commands are sent as JSON objects with `type` and optional `params`
- Responses are JSON objects with `status` and `result` or `message`

### Key Components
- **BlenderConnection class** (`server.py:26-80`): Manages socket connection to Blender
- **BlenderMCPServer class** (`addon.py:35`): Socket server running inside Blender
- **MCP Tools**: Exposed via FastMCP framework for 3D object manipulation, scene inspection, material control, and code execution

## Development Commands

### Package Management
```bash
# Install dependencies (uses UV package manager - required)
uv sync

# Run the MCP server (now with auto-installation!)
uvx blender-mcp

# Build package
uv build

# Test auto-installation system
python test_intelligent_errors.py
```

### Environment Variables
- `BLENDER_HOST`: Host address for Blender socket server (default: "localhost")  
- `BLENDER_PORT`: Port number for Blender socket server (default: 9876)

## Auto-Installation System

BlenderMCP now includes **intelligent auto-installation** that automatically detects and resolves setup issues:

### Automatic Detection & Installation
- **Detects missing Blender** and provides installation guidance
- **Auto-installs addon** when Blender is available but addon is missing
- **Auto-enables addon** if installed but disabled
- **Configures preferences** for optimal MCP usage (disables splash screen, etc.)
- **Provides clear guidance** for manual steps when needed

### Error Handling Intelligence
Instead of cryptic errors, users get helpful responses:
```
✅ Auto-Installation Successful!
BlenderMCP addon installed and enabled successfully!

Next Steps:
1. Open Blender (with GUI)  
2. Press 'N' in 3D Viewport to open sidebar
3. Find "BlenderMCP" tab  
4. Click "Connect to Claude" to start the server
5. Try your request again
```

## File Structure

```
.
├── main.py                     # Entry point wrapper
├── addon.py                   # Complete Blender addon (self-contained)
├── src/
│   └── blender_mcp/
│       ├── __init__.py
│       ├── server.py          # Main MCP server implementation
│       └── installation_manager.py # Auto-installation system
├── tests/
│   └── test_connection_scenarios.py # Connection testing
├── install_addon.py           # Standalone installation script
├── install_addon.sh          # Cross-platform install script
├── install_addon.bat         # Windows install script
├── pyproject.toml            # Project configuration and dependencies
├── AUTO_INSTALLATION_GUIDE.md # Detailed auto-installation docs
└── assets/                   # Documentation images
```

## Integration Setup

### Blender Addon Installation

#### ✅ Automatic (Recommended)
The MCP server now **auto-installs** the addon when you first use any tool:
1. Run `uvx blender-mcp` 
2. Try any BlenderMCP command in Claude
3. System detects missing addon and installs automatically
4. Follow the simple on-screen instructions

#### 📋 Manual (Fallback)
1. Run `./install_addon.sh` (Linux/macOS) or `install_addon.bat` (Windows)
2. Or install manually: Edit > Preferences > Add-ons > Install `addon.py`
3. Enable "Interface: Blender MCP" addon
4. Use BlenderMCP tab in 3D View sidebar to start server

### MCP Configuration
Add to Claude Desktop config (`claude_desktop_config.json`):
```json
{
    "mcpServers": {
        "blender": {
            "command": "uvx",
            "args": ["blender-mcp"]
        }
    }
}
```

## Key Development Notes

- **No test framework**: This project does not have automated tests
- **Socket-based architecture**: Connection must be established between MCP server and Blender addon before use
- **External API integrations**: 
  - Poly Haven API for 3D assets (requires REQ_HEADERS with User-Agent)
  - Hyper3D Rodin API for AI-generated 3D models (free trial key included)
- **Security consideration**: `execute_blender_code` tool allows arbitrary Python execution in Blender
- **Threading**: Blender addon uses daemon threads for socket server to avoid blocking UI

## Typical Development Workflow

1. Modify MCP server code in `src/blender_mcp/server.py`
2. Test with `uvx blender-mcp` 
3. Update Blender addon (`addon.py`) if needed
4. Reinstall addon in Blender for testing
5. Ensure socket connection works between components

## External Dependencies

- **Required**: UV package manager, Python 3.10+, Blender 3.0+
- **Python packages**: mcp[cli]>=1.3.0 (FastMCP framework)
- **Blender modules**: bpy, mathutils (available in Blender Python environment)