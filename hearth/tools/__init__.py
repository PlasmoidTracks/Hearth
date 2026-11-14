"""Tool registry + ToolContext.

The context carries everything a tool needs without globals: the current user id,
a callback to store download tokens, sandbox preference, device helper URL, and
workspace root. The orchestrator constructs one per turn.

Granularity model (v2): every tool has its OWN enable key `tools.<name>` and an
approval mode key `tools.<name>.approval` ("auto" | "ask"). For backwards
compatibility with older installs, if the granular key is absent from the stored
settings we fall back to the legacy coarse key (e.g. fs_list used to ride on
tools.fs_read), and approval falls back to the tool's hardcoded `gated` flag.
"""
from __future__ import annotations
import time

from .base import Tool, ToolResult  # noqa: F401
from .fs_tools import (FsReadTool, FsListTool, FsWriteTool, FsAppendTool, FsEditTool,
                       FsDeleteTool, FsMoveTool, FsSearchTool, UploadTool, ZipPackTool)
from .exec_tool import ExecTool
from .ask_user_tool import AskUserTool
from .web_search import WebSearchTool
from .web_fetch import WebFetchTool
from .time_tool import DateTimeTool
from .device_tools import DeviceListTool, DeviceScreenshotTool


class ToolContext:
    def __init__(self, user_id: str, settings: dict, download_store=None, conversation_id: str = ""):
        self.user_id = user_id
        self.conversation_id = conversation_id or ""
        self._settings = settings
        self._download_store = download_store or {}

    def workspace_root(self):
        from .. import workspaces as ws
        return ws.root_for(self.user_id)

    def sandbox_backend_pref(self) -> str:
        return self._settings.get("sandbox.backend", "auto")

    def device_helper_url(self) -> str:
        """Base URL of a paired device helper.

        Admin runtime setting wins (so it is manageable from the UI); the env var
        stays as the deployment-level fallback. Empty string == nothing paired,
        which is what DeviceListTool/DeviceScreenshotTool report to the model.
        """
        configured = str(self._settings.get("device.helper_url", "") or "").strip()
        if configured:
            return configured
        import os
        return os.environ.get("HEARTH_DEVICE_HELPER_URL", "")

    def setting(self, key: str, default=None):
        """Read an arbitrary runtime setting (e.g. 'exec.timeout_s')."""
        v = self._settings.get(key)
        return default if v is None else v

    def settings(self) -> dict:
        """The full read-only settings snapshot for this turn.

        Used by tools that need a coherent group of related settings at once
        (e.g. the web tools resolve their whole access policy from `web.*`).
        Treat the result as immutable — it is the live snapshot, not a copy.
        """
        return self._settings

    def store_download_token(self, token: str, abs_path: str, ttl: int = 3600) -> None:
        self._download_store[token] = {"path": abs_path, "exp": time.time() + ttl}

    def device_granted(self) -> bool:
        """Did the user press 'Allow device access' for this conversation?

        Read from the settings snapshot under `_device.granted`, which routes set
        after looking the grant up on the server (never from the client). When no
        helper URL is configured at all, there is nothing to gate — the device
        tools themselves report that no helper is paired.
        """
        return bool(self._settings.get("_device.granted"))

    def require_device_grant(self) -> bool:
        return bool(self._settings.get("agent.require_device_grant", True))

    def device_helper_present(self) -> bool:
        return bool(self.device_helper_url())


# Ordered list of all tool instances (singletons).
_ALL_TOOLS: list[Tool] = [
    FsReadTool(), FsListTool(), FsSearchTool(),
    FsWriteTool(), FsAppendTool(), FsEditTool(), FsDeleteTool(), FsMoveTool(),
    ZipPackTool(), UploadTool(),
    ExecTool(), AskUserTool(), WebSearchTool(), WebFetchTool(), DateTimeTool(),
    DeviceListTool(), DeviceScreenshotTool(),
]

TOOLS_BY_NAME = {t.name: t for t in _ALL_TOOLS}


def enabled_tools(settings: dict) -> list[Tool]:
    """Tools whose enable setting is on.

    Resolution order per tool: granular `tools.<name>` key if present in the
    settings dict, else legacy coarse key (setting_key) for tools that declare
    one, else ON. Tools with no key at all (ask_user) are always on.

    NOTE: when a granular spec exists (it does for every keyed tool now),
    runtime_settings.get_all() ALWAYS supplies its default in the real app, so
    the legacy fallback only matters for hand-built dicts / partial settings.
    """
    out = []
    for t in _ALL_TOOLS:
        key = t.enable_key_or_legacy()
        if not key:
            out.append(t)
            continue
        on = None
        if key in settings:
            on = bool(settings[key])
        elif t.setting_key and t.setting_key != key and t.setting_key in settings:
            on = bool(settings[t.setting_key])   # legacy fallback (e.g. tools.fs_read -> fs_list)
        if on is None or on:
            out.append(t)
    return out


def approval_mode(tool: Tool, settings: dict) -> str:
    """'auto' or 'ask' for this tool under the current settings.

    Granular `tools.<name>.approval` wins; otherwise the hardcoded Tool.gated
    flag is the default. ask_user always interrupts (it IS the interaction).
    """
    if tool.name == "ask_user":
        return "ask"
    mode = settings.get(f"tools.{tool.name}.approval")
    if isinstance(mode, str) and mode in ("auto", "ask"):
        return mode
    return "ask" if tool.gated else "auto"


def tool_specs(settings: dict) -> list[dict]:
    return [t.spec() for t in enabled_tools(settings)]
