# BlenderMCP Auto-Installation System

The BlenderMCP server now includes an **intelligent auto-installation system** that automatically detects and resolves common setup issues.

## 🚀 How It Works

When you try to use any BlenderMCP tool and encounter connection issues, the system:

1. **🔍 Diagnoses** the problem automatically
2. **🤖 Attempts auto-fix** when possible
3. **📝 Provides clear guidance** for manual steps
4. **✅ Verifies** the solution worked

## 🎯 Supported Auto-Fix Scenarios

### ✅ **Automatic Installation** 
**When**: Blender is installed but BlenderMCP addon is missing
**Action**: Automatically installs and enables the addon
**Result**: Ready to use immediately

### ✅ **Automatic Enabling**
**When**: Addon exists but is disabled  
**Action**: Enables the addon and saves preferences
**Result**: Addon activated automatically

### 📋 **Guided Manual Setup**
**When**: Blender is not installed or not running
**Action**: Provides step-by-step instructions
**Result**: Clear path to resolution

## 🔧 Error Response Examples

### Blender Not Installed
```
🔧 BlenderMCP Setup Required

🔧 **Blender Not Found**

Blender 3.0+ is required but not installed. Please:

1. **Install Blender:**
   - Download from: https://www.blender.org/download/
   - Linux: `sudo apt install blender` (or equivalent)
   - macOS: `brew install blender`
   - Windows: Download installer from official site

2. **After installation, restart this MCP server**
```

### Auto-Installation Success
```
✅ **Auto-Installation Successful!**

BlenderMCP addon installed and enabled successfully!

**Next Steps:**
1. Open Blender (with GUI)  
2. Press 'N' in 3D Viewport to open sidebar
3. Find "BlenderMCP" tab  
4. Click "Connect to Claude" to start the server
5. Try your request again

The BlenderMCP addon has been automatically installed and enabled!
```

### Blender Running But Server Not Started
```
🔧 **Blender Not Running with MCP Server**

BlenderMCP addon is installed but Blender isn't running the server.

**Next Steps:**
1. **Open Blender** (with GUI)
2. **Press 'N'** in 3D Viewport to open sidebar
3. **Find "BlenderMCP" tab**
4. **Click "Connect to Claude"** to start the server
5. **Try your request again**

The server runs on `localhost:9876` by default.
```

## 🎮 User Experience Flow

### Before Auto-Installation
1. User tries BlenderMCP tool
2. Gets cryptic error: "Connection refused"
3. Must manually figure out what's wrong
4. Follows complex installation steps
5. May give up in frustration

### With Auto-Installation  
1. User tries BlenderMCP tool
2. System detects issue and auto-fixes
3. Gets success message with clear next steps
4. Opens Blender and clicks one button
5. Everything works immediately! 🎉

## 🔬 Technical Details

### Detection Process
```python
def diagnose_connection_issue():
    # 1. Check if Blender is installed
    blender_path = find_blender_executable()
    
    # 2. Check if addon is installed  
    addon_installed = check_addon_installed(blender_path)
    
    # 3. Determine issue type and auto-fix capability
    return {
        "issue": "addon_not_installed",
        "can_auto_fix": True,
        "solution": "install_addon"
    }
```

### Auto-Installation Process
```python
def install_addon_automatically(blender_path):
    # 1. Create installation script
    # 2. Run Blender in background with script
    # 3. Install addon via bpy.ops.preferences.addon_install()
    # 4. Enable addon via bpy.ops.preferences.addon_enable()
    # 5. Save preferences via bpy.ops.wm.save_userpref()
    # 6. Verify installation success
```

## 🎯 Benefits

- **Zero Learning Curve**: Works automatically without user intervention
- **Better Error Messages**: Clear, actionable guidance instead of technical errors  
- **Faster Setup**: From 10+ manual steps to 1-2 clicks
- **Cross-Platform**: Works on Linux, macOS, and Windows
- **Fallback Safety**: Manual instructions when auto-fix isn't possible
- **No Surprises**: Always explains what it's doing

## 🔒 Security & Safety

- **Read-Only Detection**: Diagnosis process only reads system state
- **Explicit Actions**: Auto-installation only when explicitly allowed
- **Local Operations**: No network requests during installation
- **User Control**: Manual fallback always available
- **Transparent Logging**: All actions logged for debugging

## 📊 Error State Detection

The system can detect and handle:

| Issue | Detection | Auto-Fix | User Action |
|-------|-----------|----------|-------------|  
| Blender not installed | ❌ | ❌ | Install Blender |
| Addon not installed | ✅ | ✅ | None - automatic |
| Addon not enabled | ✅ | ✅ | None - automatic |
| Blender not running | ✅ | ❌ | Start Blender + server |
| Connection blocked | ✅ | ❌ | Check firewall/ports |

## 🚀 Future Enhancements

Potential improvements:
- **Auto-start Blender**: Launch Blender automatically when needed
- **Version Management**: Handle multiple Blender versions
- **Cloud Detection**: Support for cloud/remote Blender instances  
- **Dependency Checking**: Verify Python packages and system requirements
- **Update Management**: Auto-update addon when new versions available

This auto-installation system transforms BlenderMCP from a developer tool requiring technical setup into a user-friendly solution that "just works" out of the box!