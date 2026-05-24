"""Console handlers — console area operations and output scraping."""

from __future__ import annotations

import bpy


class ConsoleHandlersMixin:
    """`console_operations` and `get_console_output` commands."""

    def console_operations(self, operation, params=None):
        """Execute various console operations using bpy.ops.console

        Args:
            operation: The console operation to perform
            params: Optional parameters for the operation
        """
        try:
            # Ensure we have a console area
            console_area = None
            for area in bpy.context.screen.areas:
                if area.type == 'CONSOLE':
                    console_area = area
                    break

            if not console_area and operation != "create":
                # Try to create a console if it doesn't exist
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':  # Convert a 3D view to console
                        area.type = 'CONSOLE'
                        console_area = area
                        break

            if not console_area and operation != "create":
                return {"error": "No console area available. Use operation='create' first."}

            # Override context for console operations
            if console_area:
                override = {'area': console_area}
                for region in console_area.regions:
                    if region.type == 'WINDOW':
                        override['region'] = region
                        break

            result = {}

            # Execute the requested operation
            if operation == "create":
                # Create a new console area
                for area in bpy.context.screen.areas:
                    if area.type in ['VIEW_3D', 'TEXT_EDITOR', 'INFO']:
                        area.type = 'CONSOLE'
                        result = {"success": True, "message": "Console area created"}
                        break
                else:
                    result = {"error": "Could not create console area - no suitable area to convert"}

            elif operation == "execute":
                # Execute code in console (bpy.ops.console.execute)
                if params and "code" in params:
                    # Set the console input
                    if console_area:
                        with bpy.context.temp_override(**override):
                            # Clear current line
                            bpy.ops.console.clear_line()
                            # Insert the code
                            bpy.ops.console.insert(text=params["code"])
                            # Execute it
                            bpy.ops.console.execute()
                            result = {"success": True, "message": "Code executed in console"}
                else:
                    result = {"error": "No code provided for execution"}

            elif operation == "autocomplete":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.autocomplete()
                result = {"success": True, "message": "Autocomplete triggered"}

            elif operation == "clear":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.clear(scrollback=params.get("scrollback", True) if params else True,
                                         history=params.get("history", False) if params else False)
                result = {"success": True, "message": "Console cleared"}

            elif operation == "clear_line":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.clear_line()
                result = {"success": True, "message": "Current line cleared"}

            elif operation == "copy":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.copy()
                result = {"success": True, "message": "Text copied to clipboard"}

            elif operation == "copy_as_script":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.copy_as_script()
                result = {"success": True, "message": "Console history copied as script"}

            elif operation == "paste":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.paste()
                result = {"success": True, "message": "Text pasted from clipboard"}

            elif operation == "history_cycle":
                direction = params.get("direction", "BACKWARD") if params else "BACKWARD"
                with bpy.context.temp_override(**override):
                    bpy.ops.console.history_cycle(reverse=(direction == "FORWARD"))
                result = {"success": True, "message": f"History cycled {direction}"}

            elif operation == "history_append":
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.history_append(text=params["text"],
                                                      current_character=params.get("current_character", 0),
                                                      remove_duplicates=params.get("remove_duplicates", True))
                    result = {"success": True, "message": "Added to history"}
                else:
                    result = {"error": "No text provided for history"}

            elif operation == "insert":
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.insert(text=params["text"])
                    result = {"success": True, "message": "Text inserted"}
                else:
                    result = {"error": "No text provided for insertion"}

            elif operation == "indent":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.indent()
                result = {"success": True, "message": "Line indented"}

            elif operation == "unindent":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.unindent()
                result = {"success": True, "message": "Line unindented"}

            elif operation == "select_all":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.select_all()
                result = {"success": True, "message": "All text selected"}

            elif operation == "select_word":
                with bpy.context.temp_override(**override):
                    bpy.ops.console.select_word()
                result = {"success": True, "message": "Word selected"}

            elif operation == "scrollback_append":
                if params and "text" in params:
                    with bpy.context.temp_override(**override):
                        bpy.ops.console.scrollback_append(text=params["text"],
                                                         type=params.get("type", "OUTPUT"))
                    result = {"success": True, "message": "Added to scrollback"}
                else:
                    result = {"error": "No text provided for scrollback"}

            elif operation == "get_info":
                info = {
                    "has_console": console_area is not None,
                    "console_type": console_area.type if console_area else None,
                }

                if console_area:
                    for space in console_area.spaces:
                        if space.type == 'CONSOLE':
                            info["language"] = getattr(space, "language", "python")
                            info["font_size"] = getattr(space, "font_size", 12)
                            info["select_start"] = getattr(space, "select_start", 0)
                            info["select_end"] = getattr(space, "select_end", 0)
                            break

                result = {"success": True, "info": info}

            else:
                result = {"error": f"Unknown console operation: {operation}"}

            return result

        except Exception as e:
            return {"error": f"Console operation failed: {str(e)}"}

    def get_console_output(self, level="all", page=1, page_size=50):
        """Get recent console output from Blender's internal console with filtering and pagination

        Args:
            level: Filter by message level - "all", "info", "warning", "error", "output"
            page: Page number (1-based)
            page_size: Number of lines per page
        """
        try:
            # Store all console lines with their types
            console_lines = []

            # Helper function to classify line type
            def classify_line(line):
                """Classify line based on content and Blender's report types"""
                line_lower = line.lower()
                # Check for Blender's standard report prefixes
                if line.startswith("Error:") or 'error' in line_lower or 'exception' in line_lower or 'traceback' in line_lower:
                    return 'error'
                elif line.startswith("Warning:") or 'warning' in line_lower or 'warn' in line_lower:
                    return 'warning'
                elif line.startswith("Info:") or 'info:' in line_lower:
                    return 'info'
                elif line.startswith(">>> ") or line.startswith("... "):  # Python prompt
                    return 'input'
                else:
                    return 'output'

            # First, try to get recent operator reports using Blender's report system
            if hasattr(bpy.context.window_manager, "operators"):
                try:
                    for op in reversed(list(bpy.context.window_manager.operators)):
                        if hasattr(op, 'report'):
                            pass  # Reports are shown in UI but not directly accessible via API
                except Exception:
                    pass

            # Try to access the Python console buffer using proper API
            console_found = False
            if hasattr(bpy.context, "screen") and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'CONSOLE':
                        console_found = True
                        # Access console through proper API
                        try:
                            for space in area.spaces:
                                if space.type == 'CONSOLE':
                                    # History contains previously executed commands
                                    if hasattr(space, 'history'):
                                        for item in space.history:
                                            if hasattr(item, 'body'):
                                                text = item.body
                                                console_lines.append({
                                                    'text': text,
                                                    'type': 'input',
                                                    'source': 'history'
                                                })

                                    # Scrollback contains console output
                                    if hasattr(space, 'scrollback'):
                                        for line in space.scrollback:
                                            if hasattr(line, 'body'):
                                                text = line.body
                                                line_type = classify_line(text)
                                                if hasattr(line, 'type'):
                                                    if line.type == 'ERROR':
                                                        line_type = 'error'
                                                    elif line.type == 'INFO':
                                                        line_type = 'info'
                                                    elif line.type == 'INPUT':
                                                        line_type = 'input'
                                                    elif line.type == 'OUTPUT':
                                                        line_type = 'output'
                                                console_lines.append({
                                                    'text': text,
                                                    'type': line_type,
                                                    'source': 'console'
                                                })
                                    break
                        except Exception as e:
                            console_lines.append({
                                'text': f"(Could not access console: {e})",
                                'type': 'error',
                                'source': 'system'
                            })

            # Get Info area messages (warnings, errors from operators)
            if hasattr(bpy.context, "screen") and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'INFO':
                        console_lines.append({
                            'text': "(Info area detected - operator messages displayed in UI)",
                            'type': 'info',
                            'source': 'info_area'
                        })
                        break

            # On macOS, get system console output
            import platform
            if platform.system() == "Darwin":
                try:
                    import subprocess
                    result = subprocess.run(
                        ["log", "show", "--predicate", "process == 'Blender'", "--last", "1m"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.stdout:
                        for line in result.stdout.split('\n')[-100:]:
                            if line.strip():
                                line_type = classify_line(line)
                                console_lines.append({
                                    'text': line,
                                    'type': line_type,
                                    'source': 'system'
                                })
                except Exception as e:
                    console_lines.append({
                        'text': f"Could not access system console: {e}",
                        'type': 'warning',
                        'source': 'system'
                    })

            # Filter by level if specified
            if level != "all":
                console_lines = [line for line in console_lines if line['type'] == level]

            # Calculate pagination
            total_lines = len(console_lines)
            total_pages = (total_lines + page_size - 1) // page_size
            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_lines)

            # Get the requested page
            page_lines = console_lines[start_idx:end_idx]

            # Format output
            formatted_lines = []
            for line in page_lines:
                prefix = f"[{line['type'].upper()}]" if line['type'] != 'output' else ""
                formatted_lines.append(f"{prefix} {line['text']}" if prefix else line['text'])

            return {
                "console_output": "\n".join(formatted_lines) if formatted_lines else "No console output available",
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_lines": total_lines,
                "level": level,
                "has_console": console_found,
                "lines": page_lines  # Include structured data
            }
        except Exception as e:
            return {"error": f"Failed to get console output: {str(e)}"}
