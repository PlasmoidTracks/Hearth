"""Read-only desktop observation tools for a Linux GUI agent.

Exposed tools:
  - fetch_monitor_metadata: monitor layout plus current cursor information.
  - view_desktop: one screenshot of the full virtual desktop.
  - view_region: an exact pixel crop in virtual-desktop coordinates.

view_monitor was removed: view_desktop already captures every display at once,
and view_region covers a closer look at any single monitor's area.

No grids, markers, mouse movement, clicking, scrolling, or other input actions.
All screenshots are PNGs returned as base64 in {"text": ..., "image": ...}.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_MIN_CAPTURE_WIDTH = 10
_MIN_CAPTURE_HEIGHT = 10


def _import_mss():
    try:
        import mss
        return mss
    except ImportError:
        return None


def _new_mss(mss_module):
    return mss_module.MSS()


def _import_pil_image():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _get_cursor_position() -> Optional[Tuple[int, int]]:
    """Return the current cursor position in virtual-desktop pixels, if available."""
    try:
        from pynput import mouse
        position = mouse.Controller().position
        if isinstance(position, tuple):
            return int(position[0]), int(position[1])
        return int(position.x), int(position.y)
    except Exception:
        return None


def _png_from_grab(mss_module, region: Dict[str, int]) -> bytes:
    """Capture an MSS region and encode it as a PNG."""
    Image = _import_pil_image()
    if Image is None:
        raise RuntimeError("Pillow is not installed")

    instance = _new_mss(mss_module)
    left = int(region["left"])
    top = int(region["top"])
    right = left + int(region["width"])
    bottom = top + int(region["height"])
    shot = instance.grab((left, top, right, bottom))

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is not installed") from exc

    pixels = np.frombuffer(shot.raw, dtype=np.uint8).reshape((shot.height, shot.width, 4)).copy()
    pixels[:, :, [0, 2]] = pixels[:, :, [2, 0]]  # BGRA -> RGBA
    image = Image.fromarray(pixels, mode="RGBA").convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass(frozen=True)
class MonitorLayout:
    monitors: List[Dict[str, Any]]
    virtual_left: int
    virtual_top: int
    virtual_right: int
    virtual_bottom: int
    primary_index: int

    @property
    def virtual_width(self) -> int:
        return self.virtual_right - self.virtual_left

    @property
    def virtual_height(self) -> int:
        return self.virtual_bottom - self.virtual_top


def _remove_virtual_monitor(monitors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """MSS commonly reports an all-displays entry; remove it when individual displays exist."""
    if len(monitors) <= 1:
        return monitors

    candidates = []
    for candidate in monitors:
        contains_every_other_monitor = all(
            other is candidate
            or (
                candidate["left"] <= other["left"]
                and candidate["top"] <= other["top"]
                and candidate["left"] + candidate["width"] >= other["left"] + other["width"]
                and candidate["top"] + candidate["height"] >= other["top"] + other["height"]
            )
            for other in monitors
        )
        if contains_every_other_monitor:
            candidates.append(candidate)

    if len(candidates) == 1 and len(monitors) > 1:
        return [monitor for monitor in monitors if monitor is not candidates[0]]
    return monitors


def _get_layout() -> MonitorLayout:
    mss_module = _import_mss()
    if mss_module is None:
        raise RuntimeError("mss is not installed")

    instance = _new_mss(mss_module)
    raw_monitors = [dict(monitor) for monitor in instance.monitors]
    # mss always reports a leading pseudo-entry ({"cursor": True}) that is not a
    # display and carries no geometry. Feeding it through the layout maths below
    # raises KeyError: 'left', which surfaced as "Could not read the monitor
    # layout" for every desktop tool — so drop anything without the four
    # geometry keys before it can reach them.
    raw_monitors = [
        monitor for monitor in raw_monitors
        if all(key in monitor for key in ("left", "top", "width", "height"))
    ]
    monitors = _remove_virtual_monitor(raw_monitors)
    if not monitors:
        raise RuntimeError("no monitors detected")

    monitors.sort(key=lambda monitor: (monitor["top"], monitor["left"]))
    left = min(monitor["left"] for monitor in monitors)
    top = min(monitor["top"] for monitor in monitors)
    right = max(monitor["left"] + monitor["width"] for monitor in monitors)
    bottom = max(monitor["top"] + monitor["height"] for monitor in monitors)

    primary_index = next(
        (index for index, monitor in enumerate(monitors) if monitor.get("primary")),
        0,
    )
    return MonitorLayout(monitors, left, top, right, bottom, primary_index)


def _monitor_id(index: int) -> str:
    return f"monitor_{index + 1}"


def _monitor_containing_point(
    layout: MonitorLayout, x: int, y: int
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    for index, monitor in enumerate(layout.monitors):
        if (
            monitor["left"] <= x < monitor["left"] + monitor["width"]
            and monitor["top"] <= y < monitor["top"] + monitor["height"]
        ):
            return index, monitor
    return None, None


def _resolve_monitor(layout: MonitorLayout, monitor_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
    if monitor_id is None:
        return layout.primary_index, layout.monitors[layout.primary_index]

    if not isinstance(monitor_id, str) or not monitor_id.startswith("monitor_"):
        raise ValueError("monitor_id must be 'monitor_1', 'monitor_2', and so on")

    try:
        index = int(monitor_id.removeprefix("monitor_")) - 1
    except ValueError as exc:
        raise ValueError("monitor_id must be 'monitor_1', 'monitor_2', and so on") from exc

    if not 0 <= index < len(layout.monitors):
        raise ValueError(f"unknown monitor_id: {monitor_id!r}")
    return index, layout.monitors[index]


def _cursor_metadata(layout: MonitorLayout) -> Dict[str, Any]:
    position = _get_cursor_position()
    if position is None:
        return {"available": False}

    x, y = position
    monitor_index, monitor = _monitor_containing_point(layout, x, y)
    result: Dict[str, Any] = {
        "available": True,
        "virtual_x": x,
        "virtual_y": y,
        "monitor_id": _monitor_id(monitor_index) if monitor_index is not None else None,
    }
    if monitor is not None:
        result["monitor_x"] = x - monitor["left"]
        result["monitor_y"] = y - monitor["top"]
    return result


def _layout_text(layout: MonitorLayout, cursor: Dict[str, Any]) -> str:
    lines = [
        f"Monitor count: {len(layout.monitors)}",
        "Virtual desktop: "
        f"origin=({layout.virtual_left}, {layout.virtual_top}), "
        f"size={layout.virtual_width}x{layout.virtual_height}, "
        f"bounds=[{layout.virtual_left}, {layout.virtual_top}, "
        f"{layout.virtual_right}, {layout.virtual_bottom})",
        f"Primary monitor: {_monitor_id(layout.primary_index)}",
        "",
        "Monitors:",
    ]
    for index, monitor in enumerate(layout.monitors):
        lines.append(
            f"- {_monitor_id(index)}: origin=({monitor['left']}, {monitor['top']}), "
            f"size={monitor['width']}x{monitor['height']}, "
            f"primary={index == layout.primary_index}"
        )

    lines.append("")
    if not cursor.get("available"):
        lines.append("Mouse: unavailable (install/configure pynput if cursor metadata is needed).")
    elif cursor["monitor_id"] is None:
        lines.append(
            f"Mouse: virtual=({cursor['virtual_x']}, {cursor['virtual_y']}), "
            "outside all detected monitor rectangles."
        )
    else:
        lines.append(
            f"Mouse: virtual=({cursor['virtual_x']}, {cursor['virtual_y']}), "
            f"monitor={cursor['monitor_id']}, "
            f"monitor_relative=({cursor['monitor_x']}, {cursor['monitor_y']})."
        )
    return "\n".join(lines)


def _failure(message: str, next_step: str) -> Dict[str, Any]:
    return {
        "text": (
            f"Result: {message}\n"
            "Action status: failed\n"
            f"Next step: {next_step}"
        ),
        "image": None,
    }


TOOL_SCHEMA_fetch_monitor_metadata = {
    "type": "function",
    "function": {
        "name": "fetch_monitor_metadata",
        "description": (
            "Return the current monitor layout and mouse position. Use this for orientation "
            "before requesting a monitor or region screenshot. This tool does not capture an image."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def fetch_monitor_metadata() -> Dict[str, Any]:
    try:
        layout = _get_layout()
    except Exception as exc:
        return _failure(f"Could not read the monitor layout: {exc}", "Install mss and verify the display session allows screenshots.")

    cursor = _cursor_metadata(layout)
    return {
        "text": (
            "Result: Monitor metadata retrieved.\n"
            f"{_layout_text(layout, cursor)}\n"
            "Action status: executed\n"
            "Next step: Use view_desktop for full orientation, or view_region for a closer crop."
        ),
        "image": None,
    }


TOOL_SCHEMA_view_desktop = {
    "type": "function",
    "function": {
        "name": "view_desktop",
        "description": (
            "Capture one unmodified screenshot of the complete virtual desktop across all monitors. "
            "Use this for initial orientation. The returned image has no grid, labels, marker, or visual overlay."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def view_desktop() -> Dict[str, Any]:
    mss_module = _import_mss()
    if mss_module is None:
        return _failure("Could not capture the desktop because mss is not installed.", "Install mss and retry.")

    try:
        layout = _get_layout()
        region = {
            "left": layout.virtual_left,
            "top": layout.virtual_top,
            "width": layout.virtual_width,
            "height": layout.virtual_height,
        }
        png = _png_from_grab(mss_module, region)
    except Exception as exc:
        return _failure(f"Could not capture the virtual desktop: {exc}", "Verify that mss, Pillow, and numpy are installed and the display session permits screen capture.")

    cursor = _cursor_metadata(layout)
    return {
        "text": (
            "Result: Full virtual-desktop screenshot captured.\n"
            f"Image origin in virtual desktop: ({region['left']}, {region['top']}).\n"
            f"Image size: {region['width']}x{region['height']}.\n"
            f"{_layout_text(layout, cursor)}\n"
            "Action status: executed\n"
            "Next step: If more detail is needed, use view_region with virtual-desktop pixel coordinates."
        ),
        "image": base64.b64encode(png).decode("ascii"),
    }


TOOL_SCHEMA_view_region = {
    "type": "function",
    "function": {
        "name": "view_region",
        "description": (
            "Capture an exact unmodified crop from the virtual desktop for close inspection. "
            "x and y are always virtual-desktop pixel coordinates, including negative coordinates on monitors to the left or above the primary display. "
            "The crop is clipped to virtual-desktop bounds. No grids, markers, or overlays are added."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Left edge in virtual-desktop pixels."},
                "y": {"type": "integer", "description": "Top edge in virtual-desktop pixels."},
                "width": {"type": "integer", "description": "Requested crop width in pixels; minimum 10."},
                "height": {"type": "integer", "description": "Requested crop height in pixels; minimum 10."},
            },
            "required": ["x", "y", "width", "height"],
        },
    },
}


def view_region(args: Dict[str, Any]) -> Dict[str, Any]:
    mss_module = _import_mss()
    if mss_module is None:
        return _failure("Could not capture the region because mss is not installed.", "Install mss and retry.")

    try:
        requested_x = int(args["x"])
        requested_y = int(args["y"])
        requested_width = int(args["width"])
        requested_height = int(args["height"])
    except (KeyError, TypeError, ValueError):
        return _failure("x, y, width, and height must be integer pixel values.", "Provide all four required virtual-desktop pixel values.")

    if requested_width < _MIN_CAPTURE_WIDTH or requested_height < _MIN_CAPTURE_HEIGHT:
        return _failure(
            f"Requested crop is too small ({requested_width}x{requested_height}); minimum is {_MIN_CAPTURE_WIDTH}x{_MIN_CAPTURE_HEIGHT}.",
            "Request a crop at least 10 pixels wide and 10 pixels high.",
        )

    try:
        layout = _get_layout()
        left = max(requested_x, layout.virtual_left)
        top = max(requested_y, layout.virtual_top)
        right = min(requested_x + requested_width, layout.virtual_right)
        bottom = min(requested_y + requested_height, layout.virtual_bottom)
    except Exception as exc:
        return _failure(f"Could not read the monitor layout: {exc}", "Verify mss and the display session, then retry.")

    if right - left < _MIN_CAPTURE_WIDTH or bottom - top < _MIN_CAPTURE_HEIGHT:
        return _failure(
            "Requested crop does not overlap the virtual desktop by at least 10x10 pixels.",
            "Choose a region that overlaps a visible monitor area.",
        )

    region = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    try:
        png = _png_from_grab(mss_module, region)
    except Exception as exc:
        return _failure(f"Could not capture the requested region: {exc}", "Verify that the display session permits screen capture.")

    cursor = _cursor_metadata(layout)
    clipped = (
        requested_x != left
        or requested_y != top
        or requested_width != region["width"]
        or requested_height != region["height"]
    )
    return {
        "text": (
            "Result: Region screenshot captured.\n"
            f"Requested virtual region: ({requested_x}, {requested_y}, {requested_width}x{requested_height}).\n"
            f"Captured virtual region: ({region['left']}, {region['top']}, {region['width']}x{region['height']}).\n"
            f"Crop clipped to desktop bounds: {clipped}.\n"
            "Image-local pixel (0, 0) maps to the captured virtual-region origin above; "
            "virtual_pixel = image_local_pixel + captured virtual-region origin.\n"
            f"{_layout_text(layout, cursor)}\n"
            "Action status: executed\n"
            "Next step: Use another observation tool if a different level of detail is needed."
        ),
        "image": base64.b64encode(png).decode("ascii"),
    }


COMPUTER_TOOL_SCHEMAS = [
    TOOL_SCHEMA_fetch_monitor_metadata,
    TOOL_SCHEMA_view_desktop,
    TOOL_SCHEMA_view_region,
]


def run_computer_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a read-only desktop observation tool call."""
    args = args or {}
    if name == "fetch_monitor_metadata":
        return fetch_monitor_metadata()
    if name == "view_desktop":
        return view_desktop()
    if name == "view_region":
        return view_region(args)
    if name == "view_monitor":
        return _failure(
            "view_monitor was removed — view_desktop captures the full virtual "
            "desktop (every monitor), and view_region crops any part of it.",
            "Use view_desktop for orientation or view_region with virtual-desktop pixel coordinates.",
        )
    return _failure(f"Unknown computer tool {name!r}.", "Check the registered tool names.")
