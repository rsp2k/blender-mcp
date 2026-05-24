"""Blender message-bus (bpy.msgbus) handlers.

Subscribe/publish/notification-queue plumbing for RNA property changes.
`_msgbus_subscriptions` and `_msgbus_callbacks` are class-level dicts
(matching the original BlenderCommandExecutor); shared across instances
but in practice only one executor is constructed at a time.
"""

from __future__ import annotations

import bpy

from ..registry import command


class MsgbusHandlersMixin:
    """`msgbus_*` family of handlers."""

    # Storage for message bus subscriptions and callbacks.
    # Class-level to match the original behavior — see commit history for
    # why this isn't moved to __init__.
    _msgbus_subscriptions: dict = {}
    _msgbus_callbacks: dict = {}

    @command("msgbus_clear_by_owner")
    def msgbus_clear_by_owner(self, owner_id="default"):
        """Clear all message bus subscriptions by owner

        Args:
            owner_id: Unique identifier for the owner
        """
        try:
            # Clear all subscriptions for this owner
            bpy.msgbus.clear_by_owner(owner_id)

            return {
                "success": True,
                "message": f"Cleared all message bus subscriptions for owner: {owner_id}"
            }
        except Exception as e:
            return {"error": f"Failed to clear message bus: {str(e)}"}

    @command("msgbus_publish_rna")
    def msgbus_publish_rna(self, data_path=None, key=None):
        """Publish an RNA property change to the message bus

        Args:
            data_path: Optional data path to publish (e.g., "frame_current")
            key: Optional specific key to publish
        """
        try:
            if key:
                # Publish with specific key
                bpy.msgbus.publish_rna(key=key)
                return {
                    "success": True,
                    "message": f"Published RNA message with key: {key}"
                }
            elif data_path:
                # Try to construct key from data path
                if data_path == "frame_current":
                    key = (bpy.types.Scene, "frame_current")
                elif data_path == "active_object":
                    key = (bpy.types.ViewLayer, "objects")
                elif data_path == "selected_objects":
                    key = (bpy.types.Object, "select_set")
                elif "." in data_path:
                    # Complex paths require manual key construction
                    return {
                        "error": f"Complex data path '{data_path}' requires manual key construction"
                    }
                else:
                    return {
                        "error": f"Unknown data path '{data_path}'. Please provide a specific key."
                    }

                bpy.msgbus.publish_rna(key=key)
                return {
                    "success": True,
                    "message": f"Published RNA message for data path: {data_path}"
                }
            else:
                # Publish all pending messages
                bpy.msgbus.publish_rna()
                return {
                    "success": True,
                    "message": "Published all pending RNA messages"
                }

        except Exception as e:
            return {"error": f"Failed to publish RNA message: {str(e)}"}

    @command("msgbus_subscribe_rna")
    def msgbus_subscribe_rna(self, owner_id="default", data_path=None, notify_type="UPDATE", persistent=True):
        """Subscribe to RNA property changes via message bus

        Args:
            owner_id: Unique identifier for the subscription owner
            data_path: RNA data path to monitor (e.g., "frame_current", "active_object")
            notify_type: Type of notification (UPDATE, PERSISTENT, etc.)
            persistent: Whether the subscription persists across file loads
        """
        try:
            from bpy.types import Scene, ViewLayer, Object

            # Map common data paths to RNA keys.
            # NB: the original code also assigned `context_obj` here but never
            # read it — dead-code per ruff F841 after the move; removed.
            key = None

            if data_path == "frame_current":
                key = (Scene, "frame_current")
            elif data_path == "active_object":
                key = (ViewLayer, "objects")
            elif data_path == "selected_objects":
                key = (Object, "select_set")
            elif data_path == "active_material":
                key = (Object, "active_material")
            elif data_path.startswith("scene."):
                # Scene properties
                prop = data_path.replace("scene.", "")
                key = (Scene, prop)
            elif data_path.startswith("object."):
                # Object properties
                prop = data_path.replace("object.", "")
                key = (Object, prop)
            else:
                return {
                    "error": f"Unsupported data path: {data_path}. Supported paths: frame_current, active_object, selected_objects, active_material, scene.*, object.*"
                }

            if not key:
                return {"error": "Could not determine RNA key for data path"}

            # Create a unique subscription ID
            sub_id = f"{owner_id}_{data_path}"

            # Store the subscription info
            if owner_id not in self._msgbus_subscriptions:
                self._msgbus_subscriptions[owner_id] = {}

            # Create callback function that stores the notification
            def callback(*args):
                # Store the notification in a queue
                if sub_id not in self._msgbus_callbacks:
                    self._msgbus_callbacks[sub_id] = []

                import time
                notification = {
                    "timestamp": time.time(),
                    "data_path": data_path,
                    "owner_id": owner_id,
                    "context": str(args) if args else None
                }

                # Keep only last 100 notifications per subscription
                self._msgbus_callbacks[sub_id].append(notification)
                if len(self._msgbus_callbacks[sub_id]) > 100:
                    self._msgbus_callbacks[sub_id] = self._msgbus_callbacks[sub_id][-100:]

                # Log for debugging
                print(f"Message Bus: {data_path} changed for owner {owner_id}")

            # Subscribe to the message bus
            subscribe_options = {
                "key": key,
                "owner": owner_id,
                "args": (sub_id,),
                "notify": callback
            }

            if persistent:
                subscribe_options["options"] = {"PERSISTENT"}

            bpy.msgbus.subscribe_rna(**subscribe_options)

            # Store subscription info
            self._msgbus_subscriptions[owner_id][data_path] = {
                "key": str(key),
                "persistent": persistent,
                "notify_type": notify_type,
                "active": True
            }

            return {
                "success": True,
                "message": f"Subscribed to {data_path} for owner {owner_id}",
                "subscription_id": sub_id,
                "key": str(key)
            }

        except Exception as e:
            return {"error": f"Failed to subscribe to RNA: {str(e)}"}

    @command("msgbus_get_notifications")
    def msgbus_get_notifications(self, owner_id=None, clear=False):
        """Get pending message bus notifications

        Args:
            owner_id: Optional owner ID to filter notifications
            clear: Whether to clear notifications after reading
        """
        try:
            notifications = []

            # Filter by owner if specified
            for sub_id, notifs in self._msgbus_callbacks.items():
                if owner_id and not sub_id.startswith(owner_id + "_"):
                    continue
                notifications.extend(notifs)

            # Sort by timestamp
            notifications.sort(key=lambda x: x.get("timestamp", 0))

            # Clear if requested
            if clear:
                if owner_id:
                    # Clear only for specific owner
                    keys_to_clear = [k for k in self._msgbus_callbacks.keys()
                                    if k.startswith(owner_id + "_")]
                    for k in keys_to_clear:
                        self._msgbus_callbacks[k] = []
                else:
                    # Clear all
                    self._msgbus_callbacks.clear()

            return {
                "success": True,
                "notifications": notifications,
                "count": len(notifications)
            }

        except Exception as e:
            return {"error": f"Failed to get notifications: {str(e)}"}

    @command("msgbus_list_subscriptions")
    def msgbus_list_subscriptions(self, owner_id=None):
        """List active message bus subscriptions

        Args:
            owner_id: Optional owner ID to filter subscriptions
        """
        try:
            subscriptions = []

            if owner_id:
                if owner_id in self._msgbus_subscriptions:
                    for data_path, info in self._msgbus_subscriptions[owner_id].items():
                        subscriptions.append({
                            "owner_id": owner_id,
                            "data_path": data_path,
                            **info
                        })
            else:
                # List all subscriptions
                for owner_id_iter, paths in self._msgbus_subscriptions.items():
                    for data_path, info in paths.items():
                        subscriptions.append({
                            "owner_id": owner_id_iter,
                            "data_path": data_path,
                            **info
                        })

            return {
                "success": True,
                "subscriptions": subscriptions,
                "count": len(subscriptions),
                "owners": list(self._msgbus_subscriptions.keys())
            }

        except Exception as e:
            return {"error": f"Failed to list subscriptions: {str(e)}"}
