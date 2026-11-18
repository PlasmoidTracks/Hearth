# Architecture

## System context
Hearth is a single FastAPI process that fronts one or more `llama-server` instances (or a
built-in stub). Browsers (family devices) talk HTTPS to Hearth; Hearth talks HTTP to
llama-server on localhost; gated exec runs inside bwrap/docker; web search calls DuckDuckGo;
device tools call an optional user-run device helper over the tailnet.

```
[Family browser: mobile/desktop]
        |  HTTPS (via Tailscale / reverse proxy)   <-- TLS + auth terminate at edge
        v
[Hearth FastAPI :127.0.0.1:8080]
   |-- AuthN/AuthZ (scrypt pw, signed sessions, CSRF, rate limit, TOTP, TS-SSO)
   |-- Orchestrator (per-turn agent loop, DEC-010; abortable via cancel.py)
   |       |  streams SSE events to browser
   |-- Provider layer:  StubProvider | LlamaServerProvider --> [llama-server :8081]
   |-- Tools: fs(jail) | upload(signed dl) | web_search(DDG) | device(helper) | exec(gated)
   |                                              \--> [sandbox: bwrap/docker/stub] cwd=jail
   |-- Workspaces (data/workspaces/<uid>/  path-jailed)
   |-- SQLite (users, sessions, conversations, messages, pending_actions, docs, settings, audit)
```

## Components & responsibilities
- **main.py** — app factory, schema init, first-run admin bootstrap + doc seeding, page routes.
- **config.py / runtime_settings.py** — split boot (env, read-only) vs admin-editable (DB) config; the latter carries typed specs with labels/help/ranges for transparent model settings.
- **db.py / models.py** — SQLAlchemy 2.0 + SQLite(WAL). Tables: users, sessions, conversations, messages, pending_actions, docs, settings, audit_log.
- **security.py** — scrypt/argon2 password hashing, HMAC-signed session tokens, RFC6238 TOTP, CSRF tokens (stdlib-first, DEC-003).
- **deps.py** — current_user/admin resolution, trusted-proxy handling, CSRF enforcement.
- **ratelimit.py** — per-(ip,username) failed-login tracking + lockout.
- **workspaces.py** — canonicalize-and-contain jail; blocks absolute paths, `..`, symlink escapes.
- **sandbox.py** — auto-detect bwrap→docker→stub; run with dropped caps, no net, timeout, output caps; fail-closed if none.
- **manager.py** — provider selection + llama-server launch/health/start/stop/status; maps admin settings to CLI flags (DEC-008).
- **llm/** — Provider ABC + StreamEvent model; StubProvider; LlamaServerProvider (OpenAI-compat SSE, incremental tool_call assembly, usage/tokens-per-sec, `n_ctx` from `/props`).
- **orchestrator.py** — the agent loop; auto vs gated tool partitioning; OpenAI tool-calling fidelity (DEC-011); cancellation checks + per-turn generation options; persists history + pending actions.
- **cancel.py** — in-flight turn registry: cooperative abort by conversation, owner-scoped.
- **tools/** — fs_read/fs_list/fs_write/upload/exec/ask_user/web_search/device_*; registry + ToolContext.
- **routes_auth/chat/admin.py** — HTTP surface. Chat is SSE-over-POST for resumability.

## Data flow (one chat turn)
1. Browser POSTs `/api/chat/stream {conversation_id, content}` with session cookie + CSRF header.
2. Route loads settings + provider, appends the user message, returns an SSE stream.
3. Orchestrator builds OpenAI messages (system prompt = base persona + chosen system-prompt doc + protocol doc + workspace preamble), streams provider events to the client.
4. Auto tools execute server-side and loop; gated tools/ask_user persist a PendingAction and emit `action_required`, ending the stream.
5. User resolves via `/api/chat/resolve` → new SSE turn runs the approved tool / records the answer, then continues the loop.

## Trust boundaries
- **Browser ↔ Hearth**: untrusted client. All inputs validated; auth cookie HttpOnly+SameSite=Strict; CSRF double-submit; per-user data scoping on every query.
- **Hearth ↔ workspace**: jail helper is the choke point for all path access (DEC-005).
- **Hearth ↔ exec**: gated by explicit user approval AND sandbox isolation AND admin enablement; fail-closed without a sandbox.
- **Hearth ↔ llama-server / DDG / device helper**: outbound, localhost or user-provided URLs; timeouts + graceful degradation.

## Concurrency model
SQLite WAL allows concurrent readers; short-lived sessions per operation avoid holding locks
across long streams. Multiple conversations/users run concurrently; llama-server `--parallel`
slots bound simultaneous decodes. Single uvicorn process assumed for family scale (see
OPERATIONS.md for scaling). A turn holds exactly one registry slot per conversation
(`cancel.py`), so a second `/api/chat/stream` on the same chat is refused with 409 rather than
interleaving history.

## Key invariants
- No filesystem path resolves outside the calling user's workspace root.
- A session token is only valid if its DB row exists, is unrevoked, and unexpired.
- A gated tool never executes without a matching approved PendingAction owned by the same user.
- Model-affecting settings require an explicit Start/Restart to reach llama-server.
- **An artifact can only be downloaded by its owner** (checked at `/api/artifact/{id}`).
- Abort/retry are conversation-owner-scoped; `no_tools` can only remove capability, never grant it.
- Device tools require BOTH a paired helper and the user's per-conversation grant (unless an admin
  drops the requirement) — enforced in the tool, not in the UI.


## Session-4 components (turn control: abort / budget / visibility)
- **cancel.py** — in-process registry of in-flight turns keyed by *conversation*, one `CancelHandle`
  each. `begin()` replaces any previous handle and `end()` only unregisters if the handle is still the
  current one, so a stale turn can never wedge a chat. Routes own the handle lifetime (registered in
  `_stream_turn`, released on every exit path incl. client disconnect).
- **Abort** — `POST /api/chat/abort` sets the handle; the orchestrator checks it between provider events
  and before each tool, raising `TurnAborted`, which `run_turn` converts into a single `aborted` SSE
  event. `_stream_checked()` additionally bounds an idle provider stream.
- **Tool budget** — `agent.max_tool_steps` (runtime setting) replaces the hardcoded loop cap; `tool_call`
  events carry `step`/`of` so the UI shows `(2/6)`.
- **Activity visibility** — `tool_call` now ships a human `summary` (`_args_summary`: path + char/line
  counts, never file contents) and `awaiting_approval`; gated calls are announced *before* the approval
  interrupt instead of appearing as silence. Client renders a live activity line + one running chip per
  call, matched to its result by tool name (FIFO).
- **Turn options** — `_turn_options()` reads `{no_tools, max_tokens}` from the request body into a `gen`
  dict threaded through `run_turn`/`_agent_loop`; drives the composer's Agent-mode / Force-respond
  buttons. Can only *reduce* capability.
- **Retry** — `POST /api/chat/retry` deletes assistant/tool rows belonging to the last user turn (never
  user rows, never newer turns) and re-streams; refuses while a turn is running.
- **Context bar** — `save_turn_usage()` persists each turn's `usage` onto the newest assistant row;
  `/api/chat/context-usage` + pure `context_usage_payload()` compute used/n_ctx (real llama-server
  `n_ctx` from `/props` when available, ~4 chars/token estimate otherwise) with a ≥80 % warning.
- **Device grant** — real server-side gate: `conversations.device_granted` column (idempotent ALTER in
  `db._migrate_add_columns`), set only via `/api/chat/device-access`, injected into the settings copy as
  `_device.granted` (`_with_device_grant`) and enforced by `tools/device_tools._gate()`. Admin toggles:
  `agent.require_device_grant`, `device.helper_url`.
- **llama-server lifecycle** — `manager.start(force)` / `stop_server()` + `/api/admin/model/start|stop`;
  `force=True` relaunches so saved settings apply, and an externally managed server
  (`HEARTH_LLAMA_BASE_URL`) is never launched or killed. Admin → Model shows Start/Restart/Stop.
- **New admin surface** — Agent tab (`renderAgent`) for the tool-step budget + device-grant requirement.

## Session-2 components (files/artifacts/i18n)
- **artifacts.py + `artifacts` table** — durable records for user files (produced or uploaded);
  ownership-scoped listing + download; source of truth replacing ephemeral download tokens.
- **routes_chat upload endpoint** — `/api/upload` (multipart, CSRF, streamed, size-capped,
  filename-sanitized) writes into the caller's jail under `uploads/` and records artifacts.
- **i18n.py + static/i18n.js** — German/English string tables; per-user `language` pref on User;
  `/api/i18n` serves the table, client applies via `data-i18n*`; `<html lang>` set server-side.
- **UI (chat.html/chat.js)** — Artifacts sidebar panel, drag-&-drop + multi-file picker + camera
  capture for uploads, "Allow device access" toggle (a client-side mock in session 2; made a real
  server-side gate in session 5), language selector.

## Session-3 components (attachments/vision/conversation mgmt)
- **Attachments staging** — client `S.attachments`; `/api/chat/stream` accepts `attachments:[{artifact_id,name,mime}]`, re-resolves each via `artifacts.get(user.id,…)` and stores on the user message meta. Images become OpenAI multimodal `image_url` data URLs in `orchestrator._attachment_content`; stub `_flatten_content` renders content lists to text safely.
- **Workspace awareness** — `orchestrator._workspace_context(user_id)` appends a file listing to the system prompt.
- **Conversation mgmt** — routes for rename, delete (optional artifact purge; else detached), and search (title ilike + message-content ilike with snippet). Client: debounced search box, per-row Rename/Delete via a ⋮ menu (also right-click / long-press), Artifacts scope radio (this chat / all).

## Session-7 components (artifact identity + visible write progress)
- **artifacts.record()** — artifact identity is `(user_id, rel_path)`; re-registering an existing path
  refreshes that row in place (stable id ⇒ previously issued `/api/artifact/{id}` links keep working and
  serve the updated content). `_norm_rel()` canonicalises separators so dedupe cannot silently miss.
  `new_row=True` opts out — used by `/api/upload`, where two transfers of one filename are genuinely two
  records. Invariant: *a workspace file has one artifact row; a user upload has one row per upload.*
- **chat.js** — per-turn `Set` of `artifact_id|name` so at most one ⬇ button renders per file; write chip
  now shows path + bytes + eased estimate (`growEstimate`, client-only smoothing, never factual);
  first-run note (`#introNote`) with localStorage dismissal.
- **llm/llama_server.py** — `_prog_slot()` emits an early hint (≥32 B) then keeps the ≥256 B throttle;
  `_FIRST_STR_RE` extracts the first completed string value from the *partial* tool-call JSON for
  `path_hint`. The fragment itself remains server-side.
- **orchestrator.py** — forwards `{type:"tool_progress", name, chars, path_hint}` (truncated hint only).


## Session-8 components (download policy + anti-loop guardrails)
- **tools/fs_tools.py** — download policy lives in the tools, not the UI: `fs_write`/`fs_append` register
  the artifact and return plain text; only `upload`/`zip_pack` return `meta.download`. `fs_read` line ranges
  are verbatim (no numbering), so a read result can be quoted straight into `fs_edit.old_text`.
- **orchestrator.py** — `_call_key(tool)` (name + canonical args) plus a per-turn `{key: result}` map make a
  repeated auto-tool call non-executing; `_DONE_NOTE` records what the turn already did inside its own
  `tool_calls`, and `_clean_tool_calls()` strips it wherever stored tool_calls are replayed (message graph,
  token estimate). Audit action `tool_repeat_suppressed`.
- **runtime_settings.py / llm/llama_server.py** — `model.repetition_penalty` (default 1.0) flows through the
  existing `model.* → params` mapping and is added to the completion body only when > 1.0.
- **chat.js** — renders a ⬇ for any result carrying `meta.download` (now only upload/zip_pack do), keeping the
  per-turn dedupe as a guard against older servers; history replay resolves downloads by tool name so legacy
  transcripts cannot resurrect write-drawn buttons; activity pulse respects `(step/of)`.


## Session-9 components (Files panel actions)
- **chat.js** — the Artifacts row is just a filename link plus a **⋮ menu** (`FILE_MENU_ITEMS` → `openFileMenu`):
  download, stage-as-attachment, image preview, copy link, delete. Item list is data-driven with an optional
  `visible(art)` predicate, so adding an action is one entry and conditional items cost nothing. Handlers are
  delegated once on `#artifactList` (the panel re-renders wholesale), mirroring the conversation ⋮ menu's
  click / contextmenu / long-press trio and its deferred-outside-close trick; `findArtifact()` reads what the
  menu needs back out of the row instead of caching a list at load time, so a refresh can never desync it.
- **chat.js → stageArtifact()** — "send an existing file as an attachment" is *not* an upload: the artifact id
  goes into `S.attachments` and `/api/chat/stream` re-resolves it by owner (see SECURITY), which is the same
  path a normal staged upload takes. Deduped by id; creates a conversation first if none exists, since
  attachments are always conversation-scoped server-side.
- **routes_chat.py → delete_artifact** — gained `{purge:bool}`. Row-only deletion was the original behaviour
  and left the bytes in the jail unreachable; `purge:true` removes the file through `ws.resolve_in_jail()`
  (never a raw path) and reports `file_deleted`. Audit action `artifact_delete` records path + purged flag.
