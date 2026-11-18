"""context_usage — a meta tool: how full is the context window, right now?

Reports the same numbers the chat header's context bar shows, in both absolute tokens
and percent: tokens the model received, the window size, remaining tokens, and where
the number came from (measured by the server vs. our characters/4 estimate). It exists
because the model otherwise has no idea it is about to be truncated — with it, an agent
can say "we are at 82 %, let me summarise before continuing" instead of silently losing
the oldest turns.

Deliberately reuses `routes_chat.context_usage_payload()` rather than reimplementing the
maths: one definition of "used / window / measured-vs-estimate", so the tool can never
disagree with the bar the user is looking at. The heavy part (walking message rows, and
asking llama-server's /props for its real slot context) mirrors that endpoint.
"""
from __future__ import annotations

from .base import Tool, ToolResult


class ContextUsageTool(Tool):
    name = "context_usage"
    description = ("Report this conversation's CONTEXT-WINDOW usage: tokens used so far, the total "
                   "window (n_ctx), the percentage, and how many tokens are left — plus whether the "
                   "figure was measured by the model server or estimated. Call it before starting a "
                   "long task, and again if a reply gets cut off, to decide whether to summarise the "
                   "conversation or trim what you are about to read (large pdf_read / view_video "
                   "batches are usually the cause).")
    enable_key = "tools.context_usage"

    def parameters(self):
        return {"type": "object", "properties": {}, "required": []}

    async def run(self, args, ctx):
        conv_id = getattr(ctx, "conversation_id", "") or ""
        settings = ctx.settings()
        try:
            n_ctx_cfg = int(settings.get("model.n_ctx") or 0)
        except (TypeError, ValueError):
            n_ctx_cfg = 0

        # Last recorded usage + stored character total for this conversation. Same
        # walk the /api/chat/context-usage endpoint does; no conversation means there
        # is nothing measured, which is a legitimate answer rather than an error.
        usage: dict | None = None
        total_chars = 0
        if conv_id:
            from ..db import session_scope
            from ..models import Conversation, Message
            try:
                with session_scope() as s:
                    c = s.get(Conversation, conv_id)
                    if not c:
                        return ToolResult(False, "conversation not found")
                    rows = s.query(Message).filter(Message.conversation_id == conv_id).all()
                    for m in rows:
                        total_chars += len(m.content or "")
                        if m.role != "assistant" or not m.meta:
                            continue
                        try:
                            import json
                            mm = json.loads(m.meta)
                        except Exception:  # noqa: BLE001 — meta we cannot read is not usage
                            continue
                        if isinstance(mm.get("usage"), dict):
                            usage = mm["usage"]
            except Exception as e:  # noqa: BLE001 — a DB fault is worth reporting plainly
                return ToolResult(False, f"could not read conversation history: {str(e)[:160]}")

        # Prefer the live window size from llama-server: with --parallel>1 and no
        # unified KV cache a slot holds less than the configured total, so the setting
        # alone would overstate what is left. probe() is used because it never launches
        # a server — reporting usage must not have the side effect of booting a model.
        n_ctx = n_ctx_cfg
        provider_note = ""
        try:
            from ..manager import get_manager
            h = await get_manager().probe()
            if h.get("n_ctx"):
                n_ctx = int(h["n_ctx"])
            if not h.get("server_reachable"):
                provider_note = ("no model server answering; using the configured context length "
                                 "(and any stored usage) — figures may be estimates")
        except Exception:  # noqa: BLE001 — the bar/tool must never break on a dead provider
            provider_note = "model server unreachable; using the configured context length"

        from ..routes_chat import context_usage_payload
        d = context_usage_payload(total_chars, usage, n_ctx)
        used = int(d.get("prompt_tokens") or 0)
        pct = d.get("pct")
        lines = [f"context used: {used} tokens of {n_ctx if n_ctx else '?'} "
                 f"({(round(pct * 100, 1) if pct is not None else '?')}%)"]
        if n_ctx:
            lines.append(f"remaining: {max(0, n_ctx - used)} tokens")
        lines.append(f"measurement: {d.get('source')}"
                     + (" (characters/4 heuristic — the server reported no prompt size)"
                        if d.get("source") == "estimate" else ""))
        lines.append(f"generated last turn: {d.get('completion_tokens') or 0} tokens"
                     + (f" at {d['tokens_per_sec']} tok/s" if d.get("tokens_per_sec") else ""))
        if provider_note:
            lines.append(f"note: {provider_note}")
        if d.get("warning"):
            lines.append("WARNING: over ~80% of the window — oldest context will be lost on the "
                         "next turns. Summarise or start a new conversation before reading more "
                         "large files.")
        return ToolResult(True, "\n".join(lines), meta={
            "used": used, "n_ctx": n_ctx, "pct": pct, "source": d.get("source"),
            "warning": bool(d.get("warning"))})
