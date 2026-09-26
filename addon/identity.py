"""Persistent client identity for the message bus.

The bus uses a sticky UUID so the same Blender install is recognizable
across restarts. The UUID files live in Blender's user config dir so they
travel with the install but aren't tracked by source control.

This module imports `bpy` lazily inside the constructor — that lets the
module be importable in tests and CI without a Blender runtime, and only
hits `bpy.utils.resource_path` when an instance is actually created.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import uuid
from typing import Optional

MAX_SLOTS = 16
_LEGACY_PID_FILE = re.compile(r"^blender_mcp_uuid_(\d+)\.txt$")


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists on this machine."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) on Windows calls TerminateProcess, so ask the
        # kernel for the exit code instead.
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


class StickyUUIDManager:
    """Stable per-install client UUID that stays distinct across concurrent Blenders.

    Identity lives in numbered slots under ``<USER>/config``: slot 0 is
    ``blender_mcp_uuid.txt``, slot N is ``blender_mcp_uuid_slotN.txt``. A
    Blender claims the first slot whose lease (``<slot>.lease``, holding
    ``pid@hostname``) isn't held by another live process on this host.

    One Blender restarting reclaims slot 0 and keeps its UUID, so the bus
    sees the same client instead of a new ghost per launch. Two Blenders
    running at once get different slots, so dispatches can't land on the
    wrong scene (the reason identity was made per-process in the first
    place). Leases are never released explicitly: a dead holder pid is
    what frees a slot, which also covers crashes.

    The bus-side client ID is always prefixed with ``blender-``.
    """

    UUID_PREFIX = "blender-"

    def __init__(self, uuid_file: Optional[str] = None, config_dir: Optional[str] = None) -> None:
        if uuid_file is not None:
            # Explicit file: tests and tools that want a fixed identity.
            self.uuid_file = uuid_file
        else:
            if config_dir is None:
                import bpy  # lazy: only when actually used inside Blender
                config_dir = os.path.join(bpy.utils.resource_path("USER"), "config")
            os.makedirs(config_dir, exist_ok=True)
            try:
                self.uuid_file = self._claim_slot(config_dir)
                self._remove_stale_pid_files(config_dir)
            except Exception as e:
                # Never let identity bookkeeping block connecting; a
                # per-process uuid is the pre-slot behavior.
                print(f"[BlenderMCP] Identity slot claim failed ({e}); using a per-process uuid")
                self.uuid_file = os.path.join(config_dir, f"blender_mcp_uuid_slot_pid{os.getpid()}.txt")
        self.client_id = self._load_or_generate_uuid()

    @staticmethod
    def _slot_file(config_dir: str, slot: int) -> str:
        name = "blender_mcp_uuid.txt" if slot == 0 else f"blender_mcp_uuid_slot{slot}.txt"
        return os.path.join(config_dir, name)

    def _claim_slot(self, config_dir: str) -> str:
        me = f"{os.getpid()}@{socket.gethostname()}"
        for slot in range(MAX_SLOTS):
            uuid_path = self._slot_file(config_dir, slot)
            lease = uuid_path + ".lease"
            holder = self._read(lease)
            if holder and holder != me and self._holder_alive(holder):
                continue
            # Write-then-replace, then re-read: if two Blenders race for the
            # same free slot, the last writer wins and the other moves on.
            tmp = f"{lease}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(me)
                os.replace(tmp, lease)
            except OSError as e:
                print(f"[BlenderMCP] Could not write identity lease {lease}: {e}")
                return uuid_path
            if self._read(lease) == me:
                return uuid_path
        # Every slot held by a live process: fall back to a per-process file.
        return os.path.join(config_dir, f"blender_mcp_uuid_slot_pid{os.getpid()}.txt")

    @staticmethod
    def _holder_alive(holder: str) -> bool:
        pid_s, _, host = holder.partition("@")
        if host and host != socket.gethostname():
            # Config dir shared with another machine: can't check its pids,
            # so treat the lease as held rather than steal a live identity.
            return True
        try:
            return _pid_alive(int(pid_s))
        except ValueError:
            return False

    @staticmethod
    def _remove_stale_pid_files(config_dir: str) -> None:
        """Delete the per-pid files older builds left behind, one per launch."""
        try:
            names = os.listdir(config_dir)
        except OSError:
            return
        for name in names:
            m = _LEGACY_PID_FILE.match(name)
            if m and int(m.group(1)) != os.getpid() and not _pid_alive(int(m.group(1))):
                try:
                    os.remove(os.path.join(config_dir, name))
                except OSError:
                    pass

    @staticmethod
    def _read(path: str) -> str:
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _load_or_generate_uuid(self) -> str:
        """Load existing UUID or generate a new one."""
        stored_uuid = self._read(self.uuid_file)
        if len(stored_uuid) == 36:  # canonical UUID length
            print(f"Loaded existing client UUID: {self.UUID_PREFIX}{stored_uuid[:8]}")
            return f"{self.UUID_PREFIX}{stored_uuid}"

        new_uuid = str(uuid.uuid4())
        try:
            os.makedirs(os.path.dirname(self.uuid_file), exist_ok=True)
            with open(self.uuid_file, "w") as f:
                f.write(new_uuid)
            print(f"Generated new client UUID: {self.UUID_PREFIX}{new_uuid[:8]}")
        except Exception as e:
            print(f"Error saving UUID: {e}")

        return f"{self.UUID_PREFIX}{new_uuid}"

    def get_client_id(self) -> str:
        return self.client_id
