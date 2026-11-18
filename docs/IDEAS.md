# Ideas backlog — ported from the `llm_coder` harness comparison

These are features that were surveyed in the sibling harness (`llm_coder/`) and
**deliberately deferred** by the operator during the 2026-08 feature review. They are
recorded here so the analysis is not lost, NOT because they are approved work. Nothing
below is implemented.

Each entry names what the other harness does, where it lives there, and what has to be
decided before porting it to Hearth.

---

## 5. Context compaction (tool-result pruning) — DEFERRED
**Why deferred:** it moves bytes ahead of the new message, which is exactly the shape of
a KV-cache-invalidation bug seen in `llm_coder` (every Send re-prefills the whole previous
agent response). Until the divergence point is measured, pruning is more likely to cost
tokens than save them.

What it does there (`core/context_utils.py:compact_delete_tool_returns`): keeps the last N
tool results **per identity key** (`read_file:<path>`, `fetch_text_url:<url>`,
`web_search:<query>`, `view_video:<path>:<window>`), replaces older pairs with a
placeholder user message, protects any assistant row that still holds a kept call, and
inserts placeholders **in place** in one rebuild pass so a placeholder never lands between
an assistant `tool_calls` and its own tool result (orphaning there makes small models
hallucinate fake tool text).

Their summarizing strategy was deleted outright: *"it destroyed the conversation to save
tokens."* Keep it manual-only if ever built.

**Prerequisite:** #14 below, run on real traffic.

## 6. "Compact now" button + admin setting — DEFERRED
Depends on #5. Would sit next to the ≥80 % context-bar warning in `chat.html` and reuse
`routes_chat.context_usage_payload()`. Manual-only, never automatic.

## 12. Assistant-prefill "Continue" — DEFERRED
Keep the last assistant message as an open prefix and have the model carry on writing it
(Hearth has only Retry / force-respond). Source: `core/agent.py:prepare_continue` +
`step()`'s `_call_model`.

The trap that makes this non-trivial: the prefill request must be sent **without tools and
without chat-template kwargs**, because both change how the Jinja template renders after the
prefix and break the prefill (`--prefill-assistant` is llama.cpp's default; nothing needs to
enable it). Also needs `continuation_target()` / `_skip_control_block()` logic to find a
prose-bearing assistant row past trailing tool-call wrappers, and a merge that strips words
the model restated. Their failure mode when this went wrong: Continue *restated* the message
instead of continuing it.

## 13. Timeout clamps — DEFERRED
`_clamp_timeout(value, default, maximum)` → `max(1, min(req, max))`, one helper for every
timeout-taking tool so the ceiling cannot be forgotten on a new path; junk values get the
configured default rather than an error, and the model is never told a maximum exists.
Source: `core/agent.py:_clamp_timeout`. Hearth currently has one flat `exec.timeout_s`
(`hearth/tools/exec_tool.py`) with no per-call override, so this only matters once tools
accept timeouts from the model.

## 14. Request-boundary diff logger — DEFERRED (but see #5)
`LLM_CODER_REQDIFF=1` → fingerprints every outgoing request and reports the **first message
index at which it differs from the immediately preceding one**, plus how (added / removed /
content-differs / tool_calls-differ). That index *is* where llama.cpp's prefix reuse must
stop. Source: `core/req_diff_logger.py` (stdlib, opt-in, writes to stderr, inert by default).

**Why it matters for Hearth specifically:** `orchestrator.build_messages()` rebuilds the
system prompt every round including `_workspace_context()`, which embeds live file sizes —
so any file write changes index 0 and invalidates the entire prompt prefix. Same bug class
as the llm_coder re-prefill report, unmeasured here. A `HEARTH_REQDIFF=1` equivalent would
cost ~60 lines and settle several open questions at once.

## 15. Command allowlist — DEFERRED
Parse a command chain into base commands (`&&`, `||`, `;`, `|`, newlines, `#` comments,
leading redirections, quote-aware) and skip approval only when **every** base command is
allowlisted — so `ls && rm -rf *` still asks. Per-command add/revoke UI.
Source: `core/tools.py:parse_shell_commands` + `check_commands_allowed`,
`ui/command_approval_dialog.py`, `ui/command_whitelist_tab.py`.

Hearth's `exec` is always-ask with a bwrap/docker jail (`hearth/sandbox.py`), which is a
stricter stance than llm_coder's unsandboxed `subprocess`. An allowlist would trade some of
that for fewer interruptions on family installs — worth doing only with the sandbox still
mandatory, and it needs a per-user (not just admin) decision.

## 16. Subagents — DEFERRED
A second clean-context worker on its own `llama-server` child process, own port and own
config, because sampler / context / spec-decode / mmproj / reasoning flags are all
command-line-only and cannot be overridden per request. Read-only toolset for the worker,
capped rounds, cancellation polled (the parent cannot return to the UI mid-deployment), and
its `end_turn` must end only the subturn. Source: `core/subagent.py`,
`core/subagent_server.py` (~1000 lines including detached-spawn fallback and port-collision
diagnosis).

Needs in Hearth: a second process in `manager.py` (which currently models exactly one
child), an admin panel for the worker model, a way to render worker output inside a chat
turn, and multi-user answers the desktop version never had to ask (whose quota does a
subagent spend? is it enabled per user or globally?).

## 17. Background / asynchronous tool execution — DEFERRED
An `asynch: true` parameter injected into eligible tool schemas; a job registry
(RUNNING → DONE | ERROR | CANCELLED, terminal jobs retained until retired) so "what finished
while you weren't looking" stays answerable; results injected as messages at the same
between-rounds seam steering uses. Source: `core/async_jobs.py` (Qt-free),
`ui/async_bridge.py`, `core/agent.py:_async_dispatch`.

Interacts with #7 (implemented): the injection seam exists now, so this is additive. Not
built because it multiplies the number of ways a turn can be interrupted and Hearth's turns
are per-conversation single-flight by design.

## 18. Conversation branching / fork — DEFERRED
Deep-copy a conversation cut to N messages, `parent_id` link, auto-dedup'd branch title,
source left untouched. Source: `core/session_manager.py:fork_session` + `next_branch_title`.

Hearth equivalent would copy `messages` rows into a new `Conversation` with
`parent_conversation_id`; the awkward parts are artifacts (per-user, not per-conversation —
see DEC-040) and tool-call pairing at the cut point (cutting between an assistant
`tool_calls` row and its tool row leaves an invalid pair that llama-server rejects — same
hazard `chat_retry` already has to handle).

## 19. Tool-call stats in Admin → Tools — DEFERRED
Per-tool call counts (`ToolStats.record_call`, listener-driven) shown in the tools panel, to
inform which tools stay enabled for family accounts. Source: `core/tools.py:ToolStats`,
`ui/tools_tab.py`. Cheap; skipped only because it has no user-visible payoff yet.

## 20. `think_out_loud` / `uptime` meta-tools — DEFERRED
Both are near-free additions next to the existing `context_usage` tool
(`hearth/tools/context_tool.py`). Source: `core/tools.py` schemas +
`agent.py:handle_think_out_loud`.

Deliberately not added: they cost a tool round-trip each, and on the small local models
Hearth targets extra always-available tools measurably increase tool-call confusion. The
system prompt already asks for narration. Revisit if a stronger model becomes the default.

## 21. Draft / patch review queue — DEFERRED
Stage model edits in RAM (`DraftStore`), compound iterative edits against `base_content`,
then at review time split the unified diff into independently-acceptable hunks (close hunks
merged; above `bulk_threshold` of the file changed, offer the whole file as one patch), with
a per-hunk accept/reject UI and a `VirtualFS` read-side facade so every read tool reports
draft content identically (`[DRAFT]` marker). Source: `core/draft_store.py`,
`core/virtual_fs.py`, `ui/diff_viewer.py`.

**Largest architectural mismatch with Hearth.** Hearth's write path is
per-user-jail-direct-write + durable artifacts + a separate project-import/apply diff
(`project_apply.py`, `project_link.py`). A staging queue would either duplicate that or sit
in front of it, and the read-side facade has to be honoured by every tool at once or the
model gets contradictory answers per tool — the exact bug `virtual_fs.py` exists to fix.

---

## Also noted, not numbered (no action wanted)
- **Do not port the architecture.** llm_coder's Qt threads/signals, in-memory message list
  and single-project assumptions do not map onto Hearth's multi-user FastAPI/SQLite model.
  The turn-loop *insights* transfer; the machinery does not.
- **Their `_DONE_NOTE` / repeat-guard equivalent already exists in Hearth** and is more
  careful (see `orchestrator._call_key`, DEC-045).
- **Hearth leads** on: multi-user auth/TOTP/CSRF, workspace jail + artifacts, i18n, gated
  per-tool approval, project link/import/apply, sandboxed exec, provenance-flagged context
  bar (`measured` vs `estimate`), abort registry, argv preview + binary flag probing.
