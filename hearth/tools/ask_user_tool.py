"""ask_user tool — lets the assistant pause and ask a clarifying question.

`gated = True` in the sense that it interrupts the turn for human input, but it is
not a security gate: run() simply returns the user's answer (already captured when
the pending action was resolved). The orchestrator special-cases this tool to emit
an `ask_user` action_required event and resume with the answer.
"""
from __future__ import annotations
from .base import Tool, ToolResult


class AskUserTool(Tool):
    name = "ask_user"
    description = ("Ask the user a clarifying question and wait for their reply. Use when intent "
                   "is ambiguous or a decision is needed. Provide up to a few suggested choices.")
    gated = True   # interrupts turn for human input

    def parameters(self):
        return {"type": "object", "properties": {
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"},
                        "description": "optional suggested answers"}},
            "required": ["question"]}

    async def run(self, args, ctx):
        # The orchestrator resolves the pending ask_user and injects the answer into
        # args["answer"] before calling run(). If somehow absent, report that.
        answer = args.get("answer")
        if answer is None:
            return ToolResult(False, "no answer captured")
        return ToolResult(True, f"user answered: {answer}")
