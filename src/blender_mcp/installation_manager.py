"""
BlenderMCP Installation Manager

This module handles automatic detection and installation of the BlenderMCP addon
when connection issues are detected.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Tuple, Optional, List
import logging
import psutil
import time

logger = logging.getLogger("BlenderMCPInstaller")


class BlenderInstallationManager:
    """Manages automatic installation and troubleshooting of BlenderMCP addon"""
    
    def __init__(self):
        self.blender_paths = self._get_common_blender_paths()
        self.addon_source_path = self._find_addon_source()
    
    def _get_common_blender_paths(self) -> List[str]:
        """Get common Blender installation paths for the current platform"""
        paths = [
            "blender",  # In PATH
            "/usr/bin/blender",  # Linux system
            "/usr/local/bin/blender",  # Linux local
            "/opt/blender/blender",  # Linux opt
            "/snap/bin/blender",  # Snap
            "/var/lib/flatpak/exports/bin/org.blender.Blender",  # Flatpak
            "/Applications/Blender.app/Contents/MacOS/Blender",  # macOS
        ]
        
        # Windows paths
        if sys.platform == "win32":
            program_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            
            for version in ["4.5", "4.4", "4.3", "4.2", "4.1", "4.0", "3.6", "3.5", "3.4", "3.3", "3.2", "3.1", "3.0"]:
                paths.extend([
                    f"{program_files}\\Blender Foundation\\Blender {version}\\blender.exe",
                    f"{local_app_data}\\Programs\\Blender Foundation\\Blender {version}\\blender.exe"
                ])
        
        return paths
    
    def _find_addon_source(self) -> Optional[Path]:
        """Find the addon.py source file"""
        # Look in current directory, parent directories, etc.
        search_paths = [
            Path.cwd() / "addon.py",
            Path.cwd().parent / "addon.py",
            Path(__file__).parent.parent.parent / "addon.py"
        ]
        
        for path in search_paths:
            if path.exists():
                return path
        return None
    
    def find_blender_executable(self) -> Optional[str]:
        """Find Blender executable on the system"""
        for path in self.blender_paths:
            if shutil.which(path) or (os.path.exists(path) and os.access(path, os.X_OK)):
                try:
                    # Test if it's actually Blender
                    result = subprocess.run(
                        [path, "--version"], 
                        capture_output=True, 
                        text=True, 
                        timeout=10
                    )
                    if "Blender" in result.stdout and result.returncode == 0:
                        return path
                except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
                    continue
        return None
    
    def check_running_blender_instances(self) -> List[Dict[str, any]]:
        """Check for running Blender instances"""
        blender_processes = []
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
                try:
                    if proc.info['name'] and 'blender' in proc.info['name'].lower():
                        blender_processes.append({
                            'pid': proc.info['pid'],
                            'name': proc.info['name'],
                            'cmdline': proc.info['cmdline'],
                            'create_time': proc.info['create_time'],
                            'is_gui': '--background' not in (proc.info['cmdline'] or []) and '-b' not in (proc.info['cmdline'] or [])
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            logger.warning(f"Error checking running processes: {str(e)}")
        
        return blender_processes
    
    def is_blender_mcp_server_running(self) -> bool:
        """Check if a BlenderMCP server is likely running"""
        import socket
        try:
            # Try to connect to the default MCP port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            result = sock.connect_ex(('localhost', 9876))
            sock.close()
            return result == 0  # 0 means connection successful
        except Exception:
            return False
    
    def check_addon_installed(self, blender_path: str) -> Tuple[bool, Optional[str]]:
        """Check if BlenderMCP addon is installed in Blender"""
        try:
            # Create a simple script to check if addon is installed
            check_script = '''
import bpy
import sys

# Check if addon is in the addons
if "addon" in bpy.context.preferences.addons:
    print("ADDON_ENABLED")
    addon_info = bpy.context.preferences.addons["addon"]
    print(f"ADDON_PATH:{addon_info.module}")
else:
    print("ADDON_NOT_ENABLED")

# Check if addon file exists in scripts/addons
import os
addon_dirs = bpy.utils.script_paths("addons")
for addon_dir in addon_dirs:
    addon_file = os.path.join(addon_dir, "addon.py")
    if os.path.exists(addon_file):
        print(f"ADDON_FILE_EXISTS:{addon_file}")
        break
else:
    print("ADDON_FILE_NOT_FOUND")
'''
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(check_script)
                temp_script = f.name
            
            try:
                result = subprocess.run(
                    [blender_path, "-b", "-y", "--python", temp_script],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                output = result.stdout
                if "ADDON_ENABLED" in output:
                    return True, "Addon is installed and enabled"
                elif "ADDON_FILE_EXISTS" in output:
                    return False, "Addon file exists but is not enabled"
                else:
                    return False, "Addon is not installed"
                    
            finally:
                os.unlink(temp_script)
                
        except Exception as e:
            return False, f"Error checking addon: {str(e)}"
    
    def install_addon_automatically(self, blender_path: str) -> Tuple[bool, str]:
        """Automatically install the BlenderMCP addon"""
        if not self.addon_source_path:
            return False, "Could not find addon.py source file for installation"
        
        if not self.addon_source_path.exists():
            return False, f"Addon source file not found at: {self.addon_source_path}"
        
        # Create installation script
        install_script = f'''
import bpy
import os

addon_path = r"{self.addon_source_path.absolute()}"

try:
    # Install the addon
    bpy.ops.preferences.addon_install(filepath=addon_path, overwrite=True)
    print("INSTALL_SUCCESS")
    
    # Enable the addon
    bpy.ops.preferences.addon_enable(module="addon")
    print("ENABLE_SUCCESS")
    
    # Configure user preferences for better automation
    prefs = bpy.context.preferences
    
    # Disable splash screen for smoother startup
    prefs.view.show_splash = False
    print("SPLASH_DISABLED")
    
    # Auto-save preferences more frequently  
    prefs.filepaths.save_version = 2
    
    # Set better defaults for MCP usage
    prefs.inputs.use_mouse_emulate_3_button = True  # For users without 3-button mouse
    prefs.view.use_save_prompt = False  # Don't prompt to save on exit
    
    print("PREFERENCES_OPTIMIZED")
    
    # Save preferences
    bpy.ops.wm.save_userpref()
    print("SAVE_SUCCESS")
    
    print("INSTALLATION_COMPLETED")
    
except Exception as e:
    print(f"INSTALLATION_ERROR:{{str(e)}}")
'''
        
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(install_script)
                temp_script = f.name
            
            try:
                result = subprocess.run(
                    [blender_path, "-b", "-y", "--python", temp_script],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                
                output = result.stdout
                if "INSTALLATION_COMPLETED" in output:
                    return True, "BlenderMCP addon installed and enabled successfully!"
                elif "INSTALLATION_ERROR" in output:
                    error_line = [line for line in output.split('\n') if 'INSTALLATION_ERROR' in line][0]
                    error_msg = error_line.split(':', 1)[1] if ':' in error_line else "Unknown error"
                    return False, f"Installation failed: {error_msg}"
                else:
                    return False, f"Installation completed but verification failed. Output: {output}"
                    
            finally:
                os.unlink(temp_script)
                
        except Exception as e:
            return False, f"Error during installation: {str(e)}"
    
    def diagnose_connection_issue(self) -> Dict[str, str]:
        """Diagnose why connection to Blender is failing with smart process detection"""
        diagnosis = {
            "issue": "unknown",
            "solution": "unknown", 
            "details": "",
            "can_auto_fix": False
        }
        
        # Check if MCP server is already running
        if self.is_blender_mcp_server_running():
            diagnosis.update({
                "issue": "server_already_running",
                "solution": "connection_issue",
                "details": "BlenderMCP server appears to be running but connection failed. May be a network or firewall issue.",
                "can_auto_fix": False
            })
            return diagnosis
        
        # Check for running Blender instances
        running_instances = self.check_running_blender_instances()
        gui_instances = [p for p in running_instances if p['is_gui']]
        
        # Check if Blender is installed
        blender_path = self.find_blender_executable()
        if not blender_path:
            diagnosis.update({
                "issue": "blender_not_found",
                "solution": "install_blender", 
                "details": "Blender 3.0+ is required but not found on system",
                "can_auto_fix": False
            })
            return diagnosis
        
        diagnosis["blender_path"] = blender_path
        diagnosis["running_instances"] = len(running_instances)
        diagnosis["gui_instances"] = len(gui_instances)
        
        # Check if addon is installed
        addon_installed, addon_status = self.check_addon_installed(blender_path)
        
        if not addon_installed:
            if "not installed" in addon_status.lower():
                # Determine installation strategy based on running instances
                if gui_instances:
                    diagnosis.update({
                        "issue": "addon_not_installed_gui_running",
                        "solution": "install_addon_live",
                        "details": f"BlenderMCP addon needs installation. Found {len(gui_instances)} running Blender GUI instance(s). Live installation will be attempted.",
                        "can_auto_fix": bool(self.addon_source_path)
                    })
                else:
                    diagnosis.update({
                        "issue": "addon_not_installed",
                        "solution": "install_addon",
                        "details": f"BlenderMCP addon is not installed. {addon_status}",
                        "can_auto_fix": bool(self.addon_source_path)
                    })
            else:
                diagnosis.update({
                    "issue": "addon_not_enabled",
                    "solution": "enable_addon",
                    "details": f"BlenderMCP addon exists but not enabled. {addon_status}",
                    "can_auto_fix": True
                })
        else:
            # Addon is installed, determine why server isn't running
            if gui_instances:
                diagnosis.update({
                    "issue": "addon_installed_server_not_started",
                    "solution": "start_server_in_gui",
                    "details": f"BlenderMCP addon is installed and {len(gui_instances)} Blender GUI instance(s) running, but server not started. User needs to click 'Connect to Claude' in BlenderMCP panel.",
                    "can_auto_fix": False
                })
            else:
                diagnosis.update({
                    "issue": "addon_installed_blender_not_running",
                    "solution": "start_blender_with_addon",
                    "details": "BlenderMCP addon is installed but no Blender GUI instances are running with the server started",
                    "can_auto_fix": False
                })
        
        return diagnosis
    
    def get_setup_instructions(self, diagnosis: Dict[str, str]) -> str:
        """Get human-readable setup instructions based on diagnosis"""
        issue = diagnosis.get("issue", "unknown")
        
        instructions = {
            "blender_not_found": """
🔧 **Blender Not Found**

Blender 3.0+ is required but not installed. Please:

1. **Install Blender:**
   - Download from: https://www.blender.org/download/
   - Linux: `sudo apt install blender` (or equivalent)
   - macOS: `brew install blender`
   - Windows: Download installer from official site

2. **After installation, restart this MCP server**
""",
            
            "server_already_running": """
🔧 **Connection Issue**

A BlenderMCP server appears to be running but connection failed.

**Troubleshooting:**
1. **Check if another MCP client is connected**
2. **Verify port 9876 is not blocked by firewall**
3. **Restart Blender and try again**
4. **Check if multiple Blender instances are running**

If problems persist, restart both Blender and this MCP server.
""",
            
            "addon_not_installed_gui_running": f"""
🔧 **Live Installation Mode**

Found running Blender GUI instance(s). Installing addon in live mode...

{'**🤖 Auto-Installation Will Proceed!**' if diagnosis.get('can_auto_fix') else '**Manual Installation Required:**'}

**After installation:**
1. Look for "BlenderMCP" tab in Blender sidebar (press 'N')
2. Click "Connect to Claude" 
3. Try your request again

*Note: Live installation works without restarting Blender!*
""",
            
            "addon_installed_server_not_started": f"""
🔧 **Server Not Started**

BlenderMCP addon is installed and Blender is running, but the MCP server hasn't been started.

**Quick Fix:**
1. **In Blender**, press 'N' to open the sidebar
2. **Find the "BlenderMCP" tab**
3. **Click "🚀 Connect to Claude"**
4. **Try your request again**

*Found {diagnosis.get('gui_instances', 0)} running Blender instance(s)*
""",
            
            "addon_not_installed": f"""
🔧 **BlenderMCP Addon Not Installed**

The addon needs to be installed in Blender.

{'**🤖 Auto-Installation Available!**' if diagnosis.get('can_auto_fix') else '**Manual Installation Required:**'}

{f'''
I can automatically install it for you! The addon will be installed from:
`{self.addon_source_path}`

Alternatively, install manually:''' if diagnosis.get('can_auto_fix') else ''}

1. **Manual Installation:**
   - Open Blender
   - Go to Edit > Preferences > Add-ons
   - Click "Install..." and select `addon.py`
   - Enable "Interface: Blender MCP"

2. **Then start the server:**
   - In Blender's 3D viewport, press 'N' to open sidebar
   - Look for "BlenderMCP" tab
   - Click "Connect to Claude"
""",
            
            "addon_not_enabled": """
🔧 **BlenderMCP Addon Not Enabled**

The addon is installed but not enabled.

**🤖 I can automatically enable it for you!**

Or manually:
1. Open Blender
2. Go to Edit > Preferences > Add-ons  
3. Search for "Blender MCP"
4. Enable the checkbox
5. Start the server in the BlenderMCP sidebar tab
""",
            
            "addon_installed_blender_not_running": """
🔧 **Blender Not Running with MCP Server**

BlenderMCP addon is installed but Blender isn't running the server.

**Next Steps:**
1. **Open Blender** (with GUI)
2. **Press 'N'** in 3D Viewport to open sidebar
3. **Find "BlenderMCP" tab**
4. **Click "Connect to Claude"** to start the server
5. **Try your request again**

The server runs on `localhost:9876` by default.
""",
            
            "unknown": "Unable to diagnose the connection issue. Please check that Blender is running with the BlenderMCP addon enabled and server started."
        }
        
        return instructions.get(issue, instructions["unknown"]).strip()


# Global instance
_installation_manager = None

def get_installation_manager() -> BlenderInstallationManager:
    """Get or create the global installation manager"""
    global _installation_manager
    if _installation_manager is None:
        _installation_manager = BlenderInstallationManager()
    return _installation_manager