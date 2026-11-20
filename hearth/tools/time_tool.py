"""datetime tool — the current local date/time.

Tiny, safe, and prevents models from confidently guessing 'today'. Returns ISO
8601 local time plus weekday, using the server's clock/timezone.
"""
from __future__ import annotations
from datetime import datetime

from .base import Tool, ToolResult


class DateTimeTool(Tool):
    name = "datetime"
    label = "Date & time"
    description = ("Get the current date and time (server local timezone, ISO 8601 with weekday). "
                   "Use this instead of guessing when a message refers to today/now.")
    enable_key = "tools.datetime"

    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    async def run(self, args, ctx):
        now = datetime.now().astimezone()
        return ToolResult(True, f"{now.isoformat(timespec='seconds')} ({now.strftime('%A')})")
