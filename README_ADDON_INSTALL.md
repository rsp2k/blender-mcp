# Automated BlenderMCP Addon Installation

This directory includes scripts to automatically install the BlenderMCP addon in Blender without manual GUI interaction.

## Quick Start

### Linux/macOS
```bash
./install_addon.sh
```

### Windows
```cmd
install_addon.bat
```
Or double-click `install_addon.bat` in Windows Explorer.

### Manual Python Script
```bash
blender -b -y --python install_addon.py
```

## What the Scripts Do

1. **Find Blender**: Automatically locates Blender installation across common paths
2. **Version Check**: Verifies Blender 3.0+ compatibility
3. **Install Addon**: Uses `bpy.ops.preferences.addon_install()` to install `addon.py`
4. **Enable Addon**: Activates the addon with `bpy.ops.preferences.addon_enable()`
5. **Save Preferences**: Persists the installation with `bpy.ops.wm.save_userpref()`

## Files Created

- `install_addon.py` - Core Python installation script
- `install_addon.sh` - Linux/macOS wrapper with Blender detection
- `install_addon.bat` - Windows wrapper with Blender detection

## Advanced Usage

### Custom Addon Path
```bash
blender -b -y --python install_addon.py -- --addon-path /path/to/custom/addon.py
```

### Environment Variables
Set `BLENDER_EXECUTABLE` to override Blender detection:
```bash
export BLENDER_EXECUTABLE="/opt/blender/blender"
./install_addon.sh
```

## Supported Blender Locations

### Linux
- `/usr/bin/blender` (system package)
- `/usr/local/bin/blender` (local install)
- `/opt/blender/blender` (manual install)
- `/snap/bin/blender` (Snap package)
- `/var/lib/flatpak/exports/bin/org.blender.Blender` (Flatpak)

### macOS
- `/Applications/Blender.app/Contents/MacOS/Blender` (standard install)
- `brew` installed locations

### Windows
- `C:\Program Files\Blender Foundation\Blender X.Y\blender.exe`
- `%LOCALAPPDATA%\Programs\Blender Foundation\Blender X.Y\blender.exe`
- System PATH

## After Installation

1. Open Blender normally (with GUI)
2. Press `N` in the 3D Viewport to open the sidebar
3. Look for the **BlenderMCP** tab
4. Click **Connect to Claude** to start the server
5. Configure MCP in Claude Desktop/Cursor:

### Claude Desktop Config
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

### Cursor Config (Windows)
```json
{
    "mcpServers": {
        "blender": {
            "command": "cmd",
            "args": ["/c", "uvx", "blender-mcp"]
        }
    }
}
```

## Troubleshooting

### Blender Not Found
- Install Blender 3.0+ from [blender.org](https://www.blender.org/download/)
- Add Blender to your system PATH
- Set `BLENDER_EXECUTABLE` environment variable

### Permission Errors
- Run with elevated permissions if needed
- Check Blender addon directory permissions

### Addon Not Appearing
- Restart Blender after installation
- Check Preferences > Add-ons for "Interface: Blender MCP"
- Verify addon.py is not corrupted

### Connection Issues
- Ensure MCP server is configured in Claude/Cursor
- Check that uvx and blender-mcp package are installed: `uvx blender-mcp`
- Verify socket connection (default: localhost:9876)

## Integration with CI/CD

These scripts can be used in automated workflows:

```yaml
# GitHub Actions example
- name: Install BlenderMCP Addon
  run: |
    sudo apt-get install blender
    ./install_addon.sh
```

## Security Notes

- Scripts run Blender in background mode with Python execution enabled (`-b -y`)
- No network access required for installation
- Only modifies Blender's user preferences and addon directory
- All scripts are open source and can be audited before use