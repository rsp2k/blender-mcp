@echo off
REM BlenderMCP Addon Installation Script for Windows
REM
REM This script automates the installation of the BlenderMCP addon in Blender on Windows.
REM It handles different Blender installation locations and provides user-friendly output.
REM
REM Usage:
REM     install_addon.bat
REM     or double-click the file in Windows Explorer

setlocal enabledelayedexpansion

REM Set colors (if supported)
set "RED="
set "GREEN="
set "YELLOW="
set "BLUE="
set "NC="

REM Try to enable colors on Windows 10+
if not "%ConEmuANSI%"=="ON" (
    if not "%ANSICON%"=="" (
        set "RED=[31m"
        set "GREEN=[32m"
        set "YELLOW=[33m"
        set "BLUE=[34m"
        set "NC=[0m"
    )
)

echo %BLUE%[INFO]%NC% BlenderMCP Addon Installation Script
echo ==================================================

REM Get script directory
set "SCRIPT_DIR=%~dp0"
set "ADDON_PATH=%SCRIPT_DIR%addon.py"
set "INSTALL_SCRIPT=%SCRIPT_DIR%install_addon.py"

REM Check if addon.py exists
if not exist "%ADDON_PATH%" (
    echo %RED%[ERROR]%NC% addon.py not found at: %ADDON_PATH%
    echo %RED%[ERROR]%NC% Please ensure you're running this script from the BlenderMCP directory
    pause
    exit /b 1
)

REM Check if install_addon.py exists
if not exist "%INSTALL_SCRIPT%" (
    echo %RED%[ERROR]%NC% install_addon.py not found at: %INSTALL_SCRIPT%
    pause
    exit /b 1
)

echo %GREEN%[SUCCESS]%NC% Found addon.py at: %ADDON_PATH%

REM Find Blender executable
echo %BLUE%[INFO]%NC% Searching for Blender installation...

set "BLENDER_CMD="

REM Common Blender installation paths on Windows
set "BLENDER_PATHS[0]=blender.exe"
set "BLENDER_PATHS[1]=C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
set "BLENDER_PATHS[2]=C:\Program Files\Blender Foundation\Blender 4.1\blender.exe"
set "BLENDER_PATHS[3]=C:\Program Files\Blender Foundation\Blender 4.0\blender.exe"
set "BLENDER_PATHS[4]=C:\Program Files\Blender Foundation\Blender 3.6\blender.exe"
set "BLENDER_PATHS[5]=C:\Program Files\Blender Foundation\Blender 3.5\blender.exe"
set "BLENDER_PATHS[6]=C:\Program Files\Blender Foundation\Blender 3.4\blender.exe"
set "BLENDER_PATHS[7]=C:\Program Files\Blender Foundation\Blender 3.3\blender.exe"
set "BLENDER_PATHS[8]=C:\Program Files\Blender Foundation\Blender 3.2\blender.exe"
set "BLENDER_PATHS[9]=C:\Program Files\Blender Foundation\Blender 3.1\blender.exe"
set "BLENDER_PATHS[10]=C:\Program Files\Blender Foundation\Blender 3.0\blender.exe"
set "BLENDER_PATHS[11]=%LOCALAPPDATA%\Programs\Blender Foundation\Blender 4.2\blender.exe"
set "BLENDER_PATHS[12]=%LOCALAPPDATA%\Programs\Blender Foundation\Blender 4.1\blender.exe"
set "BLENDER_PATHS[13]=%LOCALAPPDATA%\Programs\Blender Foundation\Blender 4.0\blender.exe"

REM Check each path
for /l %%i in (0,1,13) do (
    if defined BLENDER_PATHS[%%i] (
        set "CURRENT_PATH=!BLENDER_PATHS[%%i]!"
        where "!CURRENT_PATH!" >nul 2>&1
        if !errorlevel! equ 0 (
            set "BLENDER_CMD=!CURRENT_PATH!"
            goto :found_blender
        )
        if exist "!CURRENT_PATH!" (
            set "BLENDER_CMD=!CURRENT_PATH!"
            goto :found_blender
        )
    )
)

REM Try to find blender.exe in PATH
where blender.exe >nul 2>&1
if %errorlevel% equ 0 (
    set "BLENDER_CMD=blender.exe"
    goto :found_blender
)

echo %RED%[ERROR]%NC% Blender not found!
echo %RED%[ERROR]%NC% Please install Blender 3.0+ from: https://www.blender.org/download/
echo %RED%[ERROR]%NC% Or add Blender to your system PATH
pause
exit /b 1

:found_blender
echo %GREEN%[SUCCESS]%NC% Found Blender at: %BLENDER_CMD%

REM Check Blender version
echo %BLUE%[INFO]%NC% Checking Blender version...
for /f "tokens=2" %%v in ('"%BLENDER_CMD%" --version 2^>nul ^| findstr "Blender"') do (
    echo %GREEN%[SUCCESS]%NC% Found Blender version: %%v
    REM Note: Version checking is simplified for batch - assumes modern version
)

REM Run the installation
echo %BLUE%[INFO]%NC% Installing BlenderMCP addon...
echo %BLUE%[INFO]%NC% Running: "%BLENDER_CMD%" -b -y --python "%INSTALL_SCRIPT%"

"%BLENDER_CMD%" -b -y --python "%INSTALL_SCRIPT%"

if %errorlevel% equ 0 (
    echo.
    echo %GREEN%[SUCCESS]%NC% Addon installation completed!
    echo.
    echo %BLUE%[INFO]%NC% Next steps:
    echo   1. Open Blender normally ^(with GUI^)
    echo   2. Press 'N' in the 3D Viewport to open the sidebar
    echo   3. Look for the 'BlenderMCP' tab
    echo   4. Click 'Connect to Claude' to start the server
    echo.
    echo %BLUE%[INFO]%NC% Then in Claude Desktop/Cursor, configure the MCP server:
    echo   {
    echo     "mcpServers": {
    echo         "blender": {
    echo             "command": "cmd",
    echo             "args": ["/c", "uvx", "blender-mcp"]
    echo         }
    echo     }
    echo   }
) else (
    echo %RED%[ERROR]%NC% Addon installation failed!
    echo %RED%[ERROR]%NC% Check the output above for error details
)

echo.
echo Press any key to exit...
pause >nul