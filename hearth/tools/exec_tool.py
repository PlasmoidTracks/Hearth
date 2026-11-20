"""Gated sandboxed command execution tool (DEC-004).

`gated = True`: the orchestrator will NOT call run() until the user approves this
specific invocation. When approved, run() executes inside the detected sandbox
with cwd = the user's jail and fail-closed behavior if no sandbox exists.
"""
from __future__ import annotations
from .base import Tool, ToolResult
from .. import sandbox


class ExecTool(Tool):
    name = "exec"
    label = "Command execution"
    description = ("Run a Linux shell command inside a sandbox limited to this user's workspace "
                   "(no network). Requires the user to approve each call. Use for build/test/"
                   "conversion tasks.")
    gated = True
    setting_key = "tools.exec"
    enable_key = "tools.exec"

    def parameters(self):
        return {"type": "object", "properties": {
            "command": {"type": "string", "description": "shell command to run in the jail"},
            "reason": {"type": "string", "description": "why this command is needed (shown to user)"}},
            "required": ["command"]}

    async def run(self, args, ctx):
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return ToolResult(False, "empty command")
        jail = str(ctx.workspace_root())
        backend_pref = ctx.sandbox_backend_pref()
        try:
            timeout = float(ctx.setting("exec.timeout_s", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        res = await sandbox.run_command(cmd, jail, backend_pref=backend_pref, timeout=timeout)
        body = (res.stdout or "") + (("\n[stderr]\n" + res.stderr) if res.stderr else "")
        note = f"\n[sandbox={res.backend} exit={res.exit_code} {res.duration_s:.1f}s]"
        if res.note:
            note += f"\n{res.note}"
        return ToolResult(res.ok, (body or "(no output)") + note,
                          meta={"sandbox": res.backend, "exit": res.exit_code})
