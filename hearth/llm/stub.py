"""StubProvider — a deterministic fake model so the whole app runs with no
llama.cpp installed (DEC-001). It streams tokens word-by-word, emits synthetic
prefill/eval progress + usage, and can exercise tools:

  - If the last user message contains "run <cmd>" it requests the `exec` tool.
  - If it contains "read <path>" it requests `fs_read`.
  - If it contains "list" it requests `fs_list`.
  - If it contains "search [for] <query>" / "web search …" it requests `web_search`.
  - If it contains "?" (and nothing above) it may request `ask_user`.

This lets the vertical slice (auth -> chat stream -> tool -> result) be exercised
end-to-end in a constrained environment. Tool-request parsing is intentionally
simple and documented; the real model uses native function-calling instead.
"""
from __future__ import annotations
import asyncio
import re
import time
from typing import AsyncIterator

from .base import Provider, StreamEvent, ToolRequest


class StubProvider(Provider):
    name = "stub"

    async def stream(self, messages, tools, params) -> AsyncIterator[StreamEvent]:
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = _flatten_content(m.get("content", ""))
                break

        # Prompt-size estimate for the usage event. Content may be OpenAI multimodal
        # (a list of parts), so flatten it first — str(part-dict) would otherwise dump a
        # base64 image URL into the sum and report an absurd prompt size.
        n_prompt = sum(len(_flatten_content(m.get("content", ""))) // 4 for m in messages) + 16

        # Has a tool already run for the current (last) user turn? If so, don't
        # re-request it — emit a summary reply instead. This mirrors how a real
        # model sees its own prior tool result and stops looping. Prevents the
        # "run the same tool until MAX_TOOL_LOOPS" pathology in keyword stubs.
        already_ran_tool = False
        for m in reversed(messages):
            role = m.get("role")
            if role == "user":
                break
            if role == "tool":
                already_ran_tool = True

        # synthetic prefill progress (0..1) so the debug bar is exercised
        for pct in (0.0, 0.25, 0.6, 1.0):
            yield StreamEvent("progress", data={"phase": "prefill", "pct": pct,
                                                "prompt_tokens": int(n_prompt * pct)})
            await asyncio.sleep(0.05)

        # tools are OpenAI specs: {"type":"function","function":{"name":...}}
        tool_names = {t.get("function", {}).get("name") for t in tools} if tools else set()
        low = last_user.lower()

        req = None if already_ran_tool else self._maybe_tool(low, tool_names, raw=last_user)
        if req is not None:
            # A tool round still costs tokens. Real providers report usage on their final
            # chunk, which the agent loop never waits for when a tool request ends the
            # stream — so the stub reports it here too, or the context bar and tok/s badge
            # go dead for every tool-using turn. Emit AFTER the request (a client that
            # drops events once it sees the call must not lose the numbers) and with
            # `estimated: True`, since these are keyword-stub heuristics — the orchestrator
            # only synthesises its own estimate when a provider reported nothing at all.
            args_chars = len(str(req.args))
            yield StreamEvent("tool_request", tool=req)
            yield StreamEvent("usage", data={"prompt_tokens": n_prompt,
                                             "completion_tokens": max(1, args_chars // 4),
                                             "tokens_per_sec": None,
                                             "estimated": True})
            yield StreamEvent("done", data={"finish": "tool_request", "provider": self.name})
            return

        if already_ran_tool:
            # Summarize the most recent tool result instead of re-requesting.
            last_tool_out = ""
            for m in reversed(messages):
                if m.get("role") == "tool":
                    last_tool_out = str(m.get("content", ""))[:300]
                    break
            reply = ("(stub model) Tool result: " + (last_tool_out or "(none)") +
                     "\n\nI'm running without llama.cpp — configure the backend in Admin → Model "
                     "to use a real model.")
        else:
            reply = ("(stub model) I heard you say: " + last_user.strip()[:400] +
                     ". I'm running without llama.cpp — configure the backend in Admin → Model "
                     "to use a real model. Try `list`, `read notes.md`, or `run echo hi` to see tools.")
        t0 = time.time()
        for i, word in enumerate(reply.split(" ")):
            yield StreamEvent("token", text=(word if i == 0 else " " + word))
            await asyncio.sleep(0.01)
        dur = max(0.001, time.time() - t0)
        yield StreamEvent("usage", data={"prompt_tokens": n_prompt,
                                         "completion_tokens": len(reply.split()),
                                         "tokens_per_sec": round(len(reply.split()) / dur, 1)})
        yield StreamEvent("done", data={"finish": "stop", "provider": self.name})

    def _maybe_tool(self, low: str, tool_names: set, raw: str = "") -> ToolRequest | None:
        raw = raw if raw is not None else low
        m = re.search(r"\brun\s+(.+)$", low)
        if m and "exec" in tool_names:
            return ToolRequest("exec", {"command": last_cmd(low)}, call_id="stub-exec")
        # write <path> <content...>  -> writes into the user's workspace (durable artifact)
        m = re.search(r"\bwrite\s+(\S+)\s+(.*)$", raw, re.S)
        if m and "fs_write" in tool_names:
            return ToolRequest("fs_write", {"path": m.group(1), "content": m.group(2).strip()},
                               call_id="stub-write")
        # upload <path>  -> returns a durable download link for an existing file
        m = re.search(r"\bupload\s+(\S+)", low)
        if m and "upload" in tool_names:
            return ToolRequest("upload", {"path": m.group(1)}, call_id="stub-upload")
        m = re.search(r"\bread\s+(\S+)", low)
        if m and "fs_read" in tool_names:
            path = low.split("read", 1)[1].strip().split()[0]
            return ToolRequest("fs_read", {"path": path}, call_id="stub-read")
        # search [for] <query> / web search <query> -> web_search (lets the stub exercise
        # the source-links UI end-to-end without a real model)
        m = re.search(r"(?:\bweb\s+search|\bsearch(?:ing)?(?:\s+the\s+web)?(?:\s+for)?)\s+(.+)$", low)
        if m and "web_search" in tool_names:
            return ToolRequest("web_search", {"query": m.group(1).strip()}, call_id="stub-websearch")
        # fetch/read a URL -> web_fetch
        m = re.search(r"\b(?:fetch|read)\s+(https?://\S+)", low)
        if m and "web_fetch" in tool_names:
            return ToolRequest("web_fetch", {"url": m.group(1)}, call_id="stub-webfetch")
        if re.search(r"\blist\b", low) and "fs_list" in tool_names:
            return ToolRequest("fs_list", {"path": "."}, call_id="stub-list")
        return None


def last_cmd(low: str) -> str:
    m = re.search(r"\brun\s+(.+)$", low)
    return m.group(1).strip() if m else "echo hello"


def _flatten_content(content) -> str:
    """Flatten OpenAI content (str OR multimodal list) to plain text for the stub.

    Images are represented as '[image]' so keyword tool-detection still works on the
    text parts; we never try to interpret base64 in the stub.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                out.append("[image]")
        return " ".join(out)
    return str(content)

