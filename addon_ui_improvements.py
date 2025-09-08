"""
Enhanced UI improvements for BlenderMCP addon

These improvements can be integrated into the main addon.py file to provide
better user experience and clearer guidance.
"""

# Enhanced Panel with better UX
class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "BlenderMCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        # Connection Status Section
        box = layout.box()
        box.label(text="Connection Status", icon='LINKED')
        
        if not scene.blendermcp_server_running:
            # Not connected - show prominent connect button
            col = box.column(align=True)
            col.scale_y = 1.5  # Make button bigger
            
            # Use different icons and text based on auto-setup status
            connect_op = col.operator("blendermcp.start_server", 
                                    text="🚀 Connect to Claude", 
                                    icon='PLAY')
            
            # Show helpful information
            info_box = box.box()
            info_box.label(text="💡 Once connected:", icon='INFO')
            info_col = info_box.column(align=True)
            info_col.scale_y = 0.8
            info_col.label(text="• Use Claude to create 3D content")
            info_col.label(text="• Generate models with AI")
            info_col.label(text="• Download assets automatically")
            
            # Port configuration (collapsed by default)
            box.separator()
            row = box.row()
            row.prop(scene, "blendermcp_show_advanced", text="Advanced Settings", 
                    icon='TRIA_DOWN' if scene.blendermcp_show_advanced else 'TRIA_RIGHT', 
                    emboss=False)
            
            if scene.blendermcp_show_advanced:
                advanced_box = box.box()
                advanced_box.prop(scene, "blendermcp_port", text="Port")
                advanced_box.label(text="⚠️ Only change if you know what you're doing", icon='ERROR')
                
        else:
            # Connected - show status and disconnect option
            col = box.column(align=True)
            
            # Show connection status with green indicator
            status_row = col.row()
            status_row.label(text="✅ Connected to Claude", icon='CHECKMARK')
            
            # Show port info
            col.label(text=f"Port: {scene.blendermcp_port}", icon='WORLD')
            
            # Disconnect button (smaller, less prominent)
            col.separator()
            disconnect_op = col.operator("blendermcp.stop_server", 
                                       text="Disconnect", 
                                       icon='PAUSE')
            disconnect_op.confirm = True  # Add confirmation
        
        layout.separator()
        
        # Integration Settings Section
        integrations_box = layout.box()
        integrations_box.label(text="Asset Integrations", icon='ASSET_MANAGER')
        
        # PolyHaven
        polyhaven_row = integrations_box.row()
        polyhaven_row.prop(scene, "blendermcp_use_polyhaven", text="Poly Haven")
        if scene.blendermcp_use_polyhaven:
            polyhaven_row.label(text="✅", icon='CHECKMARK')
        
        # Hyper3D
        hyper3d_row = integrations_box.row()
        hyper3d_row.prop(scene, "blendermcp_use_hyper3d", text="Hyper3D AI Models")
        if scene.blendermcp_use_hyper3d:
            if scene.blendermcp_hyper3d_api_key:
                hyper3d_row.label(text="✅", icon='CHECKMARK')
            else:
                hyper3d_row.label(text="⚠️", icon='ERROR')
                # API key input
                hyper3d_box = integrations_box.box()
                hyper3d_box.label(text="API Key Required:", icon='KEY_HLT')
                hyper3d_box.prop(scene, "blendermcp_hyper3d_api_key", text="")
                
                # Quick setup buttons
                key_row = hyper3d_box.row(align=True)
                key_row.operator("blendermcp.set_hyper3d_free_trial_api_key", 
                               text="Use Free Trial", icon='GIFT')
                key_row.operator("wm.url_open", 
                               text="Get API Key", icon='URL').url = "https://hyper3d.ai"
        
        # Sketchfab
        sketchfab_row = integrations_box.row()
        sketchfab_row.prop(scene, "blendermcp_use_sketchfab", text="Sketchfab")
        if scene.blendermcp_use_sketchfab:
            if scene.blendermcp_sketchfab_api_key:
                sketchfab_row.label(text="✅", icon='CHECKMARK')
            else:
                sketchfab_row.label(text="⚠️", icon='ERROR')
                # API key input
                sketchfab_box = integrations_box.box()
                sketchfab_box.label(text="API Key Required:", icon='KEY_HLT')
                sketchfab_box.prop(scene, "blendermcp_sketchfab_api_key", text="")
                
                # Help button
                help_row = sketchfab_box.row()
                help_row.operator("wm.url_open", 
                                text="Get Sketchfab API Key", 
                                icon='URL').url = "https://sketchfab.com/settings/password"
        
        # Help & Documentation Section
        layout.separator()
        help_box = layout.box()
        help_box.label(text="Help & Resources", icon='HELP')
        
        help_col = help_box.column(align=True)
        help_col.scale_y = 0.9
        
        # Quick help buttons
        help_row1 = help_col.row(align=True)
        help_row1.operator("wm.url_open", 
                          text="📖 Tutorial", 
                          icon='FILE_TEXT').url = "https://www.youtube.com/watch?v=lCyQ717DuzQ"
        help_row1.operator("wm.url_open", 
                          text="💬 Discord", 
                          icon='COMMUNITY').url = "https://discord.gg/z5apgR8TFU"
        
        # Status indicator for MCP server connection
        if scene.blendermcp_server_running:
            help_col.separator()
            status_box = help_col.box()
            status_box.label(text="🔗 Ready for Claude commands!", icon='LINKED')


# Enhanced Start Server Operator with better feedback
class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to Claude"
    bl_description = "Start the BlenderMCP server to enable Claude AI integration"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene

        try:
            # Create a new server instance
            if not hasattr(bpy.types, "blendermcp_server") or not bpy.types.blendermcp_server:
                bpy.types.blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

            # Start the server
            bpy.types.blendermcp_server.start()
            scene.blendermcp_server_running = True
            
            # Success feedback
            self.report({'INFO'}, f"✅ BlenderMCP server started on port {scene.blendermcp_port}")
            self.report({'INFO'}, "🚀 Ready for Claude AI commands!")
            
            # Show instructions in the UI
            context.area.tag_redraw()
            
        except Exception as e:
            scene.blendermcp_server_running = False
            self.report({'ERROR'}, f"❌ Failed to start server: {str(e)}")
            self.report({'ERROR'}, "💡 Try changing the port number in Advanced Settings")

        return {'FINISHED'}


# Enhanced Stop Server Operator with confirmation
class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from Claude"
    bl_description = "Stop the BlenderMCP server and disconnect from Claude"
    bl_options = {'REGISTER'}
    
    confirm: bpy.props.BoolProperty(name="Confirm Disconnect", default=True)

    def invoke(self, context, event):
        if self.confirm:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        
        try:
            if hasattr(bpy.types, "blendermcp_server") and bpy.types.blendermcp_server:
                bpy.types.blendermcp_server.stop()
                
            scene.blendermcp_server_running = False
            self.report({'INFO'}, "🔌 Disconnected from Claude")
            
            # Redraw UI
            context.area.tag_redraw()
            
        except Exception as e:
            self.report({'WARNING'}, f"⚠️ Error stopping server: {str(e)}")
            scene.blendermcp_server_running = False

        return {'FINISHED'}


# Additional property for UI state
def register_ui_improvements():
    """Register the UI improvements (add to main register function)"""
    
    # Advanced settings visibility toggle
    bpy.types.Scene.blendermcp_show_advanced = bpy.props.BoolProperty(
        name="Show Advanced Settings",
        description="Show advanced configuration options",
        default=False
    )