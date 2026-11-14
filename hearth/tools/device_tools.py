"""Device access tools — read-only folder + screenshot, device-agnostic.

These talk to a small *device helper* the user may run on their own machine/phone
over the same tailnet. The helper exposes two endpoints:
  GET {helper_url}/folder?path=<rel>   -> list/read files in an allow-listed folder
  GET {helper_url}/screenshot          -> PNG bytes (or a link)

No helper ships with Hearth, so until one is paired these tools fail gracefully
with a clear message. The interface + settings plumbing are real: enable per tool
in Admin → Tools → Device and set the helper URL there (or HEARTH_DEVICE_HELPER_URL).

Two independent preconditions gate these tools (both enforced here, not in the UI):
  1. A helper must be paired (`device.helper_url` admin setting or the env var).
  2. The user must have granted device access for this conversation — the "Allow
     device access" button in the chat header, persisted on the conversation row
     (DEC-034). Skippable by admin via Admin → Agent → Require device-access grant.

User uploads/downloads are NOT device access and never pass through here.
"""
from __future__ import annotations
import httpx

from .base import Tool, ToolResult

_NO_HELPER = ("No device helper is paired, so there is nothing to read or capture. An admin must run a "
              "helper and set its URL (Admin → Tools → Device → Device helper URL, or "
              "HEARTH_DEVICE_HELPER_URL), then try again.")
_NO_GRANT = ("Device access has not been granted for this chat. Ask the user to press "
             "'Allow device access' in the chat header first.")


def _gate(ctx) -> str:
    """Return a refusal message, or '' when the tool may proceed.

    Order matters for what the *user* can act on: no helper is an admin problem and
    the only true blocker, so it is reported first even when the grant is missing —
    telling them to press a button that cannot help would be misleading.
    """
    if not ctx.device_helper_present():
        return _NO_HELPER
    if ctx.require_device_grant() and not ctx.device_granted():
        return _NO_GRANT
    return ""


class DeviceListTool(Tool):
    name = "device_list"
    description = ("List files in the user's allow-listed device folder (read-only). Requires a "
                   "paired device helper.")
    setting_key = "tools.device"      # legacy fallback key
    enable_key = "tools.device_list"

    def parameters(self):
        return {"type": "object", "properties": {
            "path": {"type": "string", "description": "relative path inside the shared folder"}},
            "required": []}

    async def run(self, args, ctx):
        deny = _gate(ctx)
        if deny:
            return ToolResult(False, deny)
        url = ctx.device_helper_url()
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{url.rstrip('/')}/folder", params={"path": args.get("path", "")})
                return ToolResult(r.status_code == 200, r.text[:8000])
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"device helper unreachable: {str(e)[:160]}")


class DeviceScreenshotTool(Tool):
    name = "device_screenshot"
    description = ("Capture a screenshot from the user's paired device (read-only). Requires a "
                   "paired device helper. Returns an image the UI can display.")
    setting_key = "tools.device"      # legacy fallback key
    enable_key = "tools.device_screenshot"
    gated = True                      # screen grabs always ask

    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    async def run(self, args, ctx):
        deny = _gate(ctx)
        if deny:
            return ToolResult(False, deny)
        url = ctx.device_helper_url()
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{url.rstrip('/')}/screenshot")
                if r.status_code != 200:
                    return ToolResult(False, f"device helper error {r.status_code}")
                # Persist into workspace so it can be shown/downloaded.
                from .. import workspaces as ws
                rel = ws.write_bytes(ctx.user_id, "screenshots/latest.png", r.content)
                return ToolResult(True, f"captured screenshot -> {rel}",
                                  meta={"image": {"path": rel}})
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"device helper unreachable: {str(e)[:160]}")
