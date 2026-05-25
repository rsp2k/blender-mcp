#!/bin/bash
# BlenderMCP Addon Installation Script
#
# This script automates the installation of the BlenderMCP addon in Blender.
# It handles different Blender installation locations and provides user-friendly output.
#
# Usage:
#     ./install_addon.sh
#     or
#     bash install_addon.sh

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to find Blender executable
find_blender() {
    local blender_paths=(
        "blender"                    # In PATH
        "/usr/bin/blender"          # Linux system install
        "/usr/local/bin/blender"    # Linux local install
        "/opt/blender/blender"      # Linux opt install
        "/Applications/Blender.app/Contents/MacOS/Blender"  # macOS
        "/snap/bin/blender"         # Snap package
        "/var/lib/flatpak/exports/bin/org.blender.Blender"  # Flatpak
    )
    
    for path in "${blender_paths[@]}"; do
        if command -v "$path" &> /dev/null; then
            echo "$path"
            return 0
        fi
    done
    
    return 1
}

# Function to check Blender version
check_blender_version() {
    local blender_cmd="$1"
    local version_output
    
    version_output=$("$blender_cmd" --version 2>/dev/null | head -n1)
    if [[ $version_output =~ Blender\ ([0-9]+\.[0-9]+) ]]; then
        local major_minor="${BASH_REMATCH[1]}"
        local major="${major_minor%%.*}"
        local minor="${major_minor##*.}"
        
        if (( major > 3 || (major == 3 && minor >= 0) )); then
            print_success "Found compatible Blender version: $version_output"
            return 0
        else
            print_warning "Found Blender version $major_minor, but BlenderMCP requires 3.0+"
            return 1
        fi
    else
        print_warning "Could not determine Blender version from: $version_output"
        return 1
    fi
}

# Main installation function
install_addon() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local addon_pkg="$script_dir/addon"
    local addon_shim="$script_dir/addon.py"
    local install_script="$script_dir/install_addon.py"

    print_status "BlenderMCP Addon Installation Script"
    echo "=================================================="

    # Need at least one of: addon/ package directory or addon.py shim.
    # install_addon.py prefers the package when both are present.
    if [[ ! -d "$addon_pkg/" && ! -f "$addon_shim" ]]; then
        print_error "Neither addon/ nor addon.py found alongside $0"
        print_error "Please run this script from the BlenderMCP project directory"
        exit 1
    fi

    if [[ ! -f "$install_script" ]]; then
        print_error "install_addon.py not found at: $install_script"
        exit 1
    fi

    if [[ -d "$addon_pkg/" ]]; then
        print_success "Found addon/ package at: $addon_pkg (preferred)"
    else
        print_success "Found addon.py shim at: $addon_shim"
    fi
    
    # Find Blender
    print_status "Searching for Blender installation..."
    local blender_cmd
    if blender_cmd=$(find_blender); then
        print_success "Found Blender at: $blender_cmd"
    else
        print_error "Blender not found!"
        print_error "Please install Blender 3.0+ or add it to your PATH"
        print_error ""
        print_error "Installation options:"
        print_error "  - Download from: https://www.blender.org/download/"
        print_error "  - Linux: sudo apt install blender  # or equivalent"
        print_error "  - macOS: brew install blender"
        print_error "  - Snap: sudo snap install blender --classic"
        exit 1
    fi
    
    # Check Blender version
    print_status "Checking Blender version..."
    if ! check_blender_version "$blender_cmd"; then
        print_error "Incompatible Blender version. Please install Blender 3.0 or newer."
        exit 1
    fi
    
    # Run the installation
    print_status "Installing BlenderMCP addon..."
    print_status "Running: $blender_cmd -b -y --python \"$install_script\""
    
    if "$blender_cmd" -b -y --python "$install_script"; then
        print_success "Addon installation completed!"
        echo ""
        print_status "Next steps:"
        echo "  1. Open Blender normally (with GUI)"
        echo "  2. Press 'N' in the 3D Viewport to open the sidebar"
        echo "  3. Look for the 'BlenderMCP' tab"
        echo "  4. Click 'Connect to Claude' to start the server"
        echo ""
        print_status "Then in Claude Desktop/Cursor, configure the MCP server:"
        echo '  {
    "mcpServers": {
        "blender": {
            "command": "uvx",
            "args": ["blender-mcp"]
        }
    }
  }'
    else
        print_error "Addon installation failed!"
        print_error "Check the output above for error details"
        exit 1
    fi
}

# Check if script is being sourced or executed
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    install_addon "$@"
fi