"""Tool base + result type."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    ok: bool
    content: str                 # text fed back to the model as the tool message
    meta: dict | None = None     # extra UI info (e.g. download link)


class Tool(ABC):
    name: str = ""
    description: str = ""
    gated: bool = False          # default approval: True == "ask" before running
    setting_key: str = ""        # legacy coarse enable key (fallback below)
    enable_key: str = ""         # granular per-tool enable key (preferred)

    def enable_key_or_legacy(self) -> str:
        return self.enable_key or self.setting_key

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }

    @abstractmethod
    def parameters(self) -> dict:
        ...

    @abstractmethod
    async def run(self, args: dict, ctx: "ToolContext") -> ToolResult:  # noqa: F821
        ...
