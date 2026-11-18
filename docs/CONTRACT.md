# Implementation Contract — LOCKED

Project: **Hearth** — a self-hosted LLM harness ("llm harness engine") using llama.cpp as backbone, for family use.
Contract version/date: v1.0 — locked at intake (session start)
Mode: greenfield

## Outcome
A secure, multi-user, streaming local-LLM chat server with per-user workspaces, gated tools, web search, admin-managed config/protocols/system-prompts, and a responsive mobile+desktop UI. Built to run on a Linux server, reachable by family from outside the LAN via an authenticated tunnel (Tailscale-first guidance).

## Primary users / operators
- Family members (chat + own workspace)
- Admin (you): manage users, model settings, protocols (.md), system prompts (.md), config.

## Included scope (must-have)
1. llama.cpp backbone via `llama-server` subprocess(es); OpenAI-compatible HTTP client. Stub provider so the app runs with **no** llama.cpp installed (working prototype).
2. User management / auth: local accounts, scrypt password hashing (stdlib), signed session tokens, optional per-user TOTP 2FA, Tailscale identity SSO header (optional), brute-force rate limiting + lockout, CSRF, TLS-only cookie guidance, bind-to-localhost default.
3. Per-user isolated workspace the LLM can see (path-jailed filesystem).
4. Tools: read file, write file, list dir, upload/produce downloadable file, gated sandboxed command execution, ask-user-a-question, web search, device access (read-only folder + screenshot) — device tools via a helper protocol, stubbed.
5. Streaming output (SSE) with prefill/eval progress + token/s telemetry + tool-call debug view.
6. Admin panel: users, model settings (transparent, documented values), protocols (.md) selectable by users, system prompts (.md), runtime config. RoPE extension / context controls; default n_ctx 262144.
7. Concurrent multi-user usage.

## Non-goals
- Not a general-purpose agent OS; not multi-tenant SaaS billing; no Kubernetes/Helm; no model training/fine-tuning UI; no native mobile app (responsive web instead). Device tools are stubbed behind a documented helper protocol.

## Target environment
Linux server (x86_64/ARM), Python 3.10+. Optional GPU depends on llama.cpp build. Sandbox auto-detect bwrap→docker→stub.

## Target maturity
Integrated **prototype / release-candidate** for family use. Security-sensitive paths implemented + reviewed, but external-exposure hardening is documented guidance (operator runs TLS/tunnel).

## Deliverables
Source tree + requirements.txt + run instructions + admin/user docs + self-test script. Runnable via uvicorn with stub LLM out of the box.

## Success criteria
- `uvicorn` boots; login works; chat streams token-by-token from stub or llama-server; fs/exec/ask/web tools execute against a user's jail; admin can edit config/protocols/prompts and they take effect.

## Unacceptable outcomes
Path traversal out of a user's workspace; unauthenticated access to another user's data; command exec escaping its sandbox without gating; secrets committed/logged.

## Known constraints
- This build session has **no shell/execute tool**, so runtime verification (booting the server) is performed by the operator via provided commands, not by the agent. See Delivery Record for evidence status per requirement (Rule 4).
- llama.cpp presence/version on target unknown → runtime auto-detection + stub fallback.

## Quality-pack matrix
- Functional correctness: REQUIRED
- Unit/component tests: ENABLED (self-test script, stdlib unittest)
- Contract/API tests: AUTO (API smoke in self-test)
- Integration tests: ENABLED (workspace jail, tool gating)
- End-to-end tests: AUTO (SSE chat smoke via TestClient where deps allow)
- Property/fuzz/mutation/load/chaos: OFF (out of prototype budget; noted as follow-ups)
- Security & dependency checks: REQUIRED (manual review + secure-by-default code)
- Privacy review: ENABLED (data minimization, per-user isolation)
- Accessibility: ADVISORY (semantic HTML, keyboard-operable UI)
- Compatibility/portability: AUTO (Linux focus)
- Migration/backup/restore: ENABLED (SQLite; documented backup)
- Observability: REQUIRED (structured logs, audit trail, debug panel)
- Supply-chain/SBOM/provenance: OFF (prototype)
- User acceptance: DEFERRED to operator

## Optional-pack matrix
CI: AUTO(off now). CD/infra/publishing/prod deploy: OFF. DB migrations: simple versioned schema init. Feature flags: light (per-tool enable). Telemetry: local audit only. i18n: OFF. a11y impl: basic. Threat modeling/hardening: ENABLED. Backup/restore: documented. Runbooks: provided.

## Action authority matrix
- A0 inspect / A1 reversible local edits + tests: autonomous (mode=full_auto).
- A2 reversible external (PRs/issues): restricted (none requested).
- A3 consequential (deploy/publish/prod data): approval gate — none in scope now.
- A4 destructive/legal: never inferred.

## Predeclared gates
None active for this prototype delivery. External-exposure deployment = operator action, not agent.

## Cost/time/resource budget
No paid services. No external network calls at runtime except web search (user-triggered) and optional model download (operator).

## Ambiguity policy
Smallest reversible choice consistent with requirements + conventions; record in DECISIONS.md.

## Failure policy
Repair proportionally; otherwise deliver with visible exception. Security defects block relevant claims.

## Progress/reporting policy
No routine questions post-lock. Artifacts + final Delivery Record.

## Defaulted choices (stated)
- Password hashing: stdlib `hashlib.scrypt` (argon2 used automatically if `argon2-cffi` present).
- Sessions: HMAC-signed stateful tokens w/ server-side revocation list in DB.
- DB: SQLite via SQLAlchemy 2.0.
- Frontend: Jinja2 + HTMX + Tailwind (CDN, no build step).
- Web search: DuckDuckGo HTML endpoint via httpx, graceful failure.

## Open controlled assumptions
- ASM1 target is Linux w/ Python 3.10+ (sensitivity high; verified by operator `python3 --version`).
- ASM2 llama.cpp optional; stub guarantees runnability (verified by self-test).
- ASM3 sandbox backend availability varies; auto-detect + gating mitigates (PARTIALLY VERIFIED until operator runs it).

## Question Closure Statement
All ordinary product and engineering choices are now delegated within this contract. No further user input will be requested unless a predeclared gate triggers, required authority or secret is missing, new evidence creates material risk outside the contract, or the requested outcome becomes impossible or contradictory.

---

## Contract amendment v1.1 — operator-requested additions (session 2)
Raised by operator after first verified boot; executed autonomously while operator AFK.

| REQ | Capability | Priority | Acceptance criteria | Verification | Status |
|---|---|---|---|---|---|
| REQ-020 | Per-user private workspace demonstrable via stub: `write <path> <text>` writes into caller's jail only; agent sees only that user's space | must | write→file in `<uid>/`; other users can't list/read it | selftest write-artifact + existing jail tests | Implemented; static-reviewed (runtime pending) |
| REQ-021 | Agent-produced file → durable download link shown in chat + session Artifacts panel | must | upload/write yield `/api/artifact/{id}`; listed per conversation; survives restart | selftest upload-tool + write-artifact | Implemented; static-reviewed |
| REQ-022 | User uploads files (device→server): drag-drop desktop, multi-file picker desktop+mobile, image gallery/camera mobile | must | POST /api/upload stores in jail, size-capped, sanitized, recorded as artifact | selftest upload HTTP test + manual UI | Implemented; static-reviewed |
| REQ-023 | "Allow device access" grant flow integrated (mocked), single-file upload as concrete resource path | should | toggle records grant + informs transcript; device tools remain stubs behind helper protocol | manual UI | Implemented (mock) → **superseded by REQ-037** (real server-side gate, session 5); UNVERIFIED |
| REQ-024 | Session artifacts list re-downloadable afterwards (durable) | must | /api/artifacts lists owned files by conversation; download enforces ownership | selftest ownership enforcement | Implemented; static-reviewed |
| REQ-025 | Language setting (German + English), per-user, applied to UI chrome | must | user picks de/en; UI strings localize; default en; every EN key has DE | selftest i18n completeness | Implemented; static-reviewed |

Non-goals reaffirmed: no AV scanning of uploads; device access is mocked (no real host/phone capture yet — session 5 made the *grant* real, the helper daemon is still unshipped); model *output* language not forced by the setting.
Amendment note: these extend scope additively; no prior requirement narrowed. Authority: A1 reversible local edits (full_auto). No A2–A4 actions taken.

### Amendment v1.2 — operator-requested UX batch (session 3)
| REQ | Capability | Priority | Verification | Status |
|---|---|---|---|---|
| REQ-026 | Attachments staged client-side; sent only with the message (no per-file model reply); attachments-only messages allowed | must | manual UI | Implemented; UNVERIFIED |
| REQ-027 | Clear upload errors incl. explicit "too large" (client pre-check + server 413 passthrough) | must | selftest upload + manual | Implemented; UNVERIFIED |
| REQ-028 | Uploaded images sent as multimodal base64 data URLs, never raw bytes in text | must | selftest `_attachment_content` round-trip + stub flatten | Implemented; UNVERIFIED |
| REQ-029 | Model sees conversation's workspace files (system-prompt file listing) | must | selftest `_workspace_context` | Implemented; UNVERIFIED |
| REQ-030 | "All artifacts" collection view (not just current chat) | must | manual UI | Implemented; UNVERIFIED |
| REQ-031 | Delete conversation, asking whether to delete its artifacts too (else detached/kept) | must | selftest HTTP delete + manual | Implemented; UNVERIFIED |
| REQ-032 | Rename conversation | must | selftest HTTP rename | Implemented; UNVERIFIED |
| REQ-033 | Search conversations: title match + secondary message-content match with snippet | should | selftest HTTP search | Implemented; UNVERIFIED |

New endpoints: `POST /api/chat/conversations/{id}/rename`, `…/delete` (body `{delete_artifacts}`), `GET /api/chat/search?q=`. Chat stream now accepts `attachments:[{artifact_id,name,mime}]`.

### Amendment v1.3 — operator-requested turn-control batch (session 5)
Raised by the operator from real use (`user.txt`); executed autonomously, runtime UNVERIFIED.

| REQ | Capability | Priority | Acceptance criteria | Verification | Status |
|---|---|---|---|---|---|
| REQ-034 | Abort button stops an in-flight turn (UI + server), leaving history coherent and reporting `aborted` | must | Stop/Escape halts generation; chip/activity cleared; Send re-enabled; audit `chat_abort`; only the owner can abort | selftest registry + abort-event test; manual | Implemented; UNVERIFIED |
| REQ-035 | Admin setting for max tool uses per turn | must | `agent.max_tool_steps` (1–50) bounds the loop; over-limit message names the setting | selftest `_max_steps`; manual with cap=2 | Implemented; UNVERIFIED |
| REQ-036 | Visualise background work: show that/what the model is doing while a tool runs | must | live activity line + running chip per call w/ arg summary + `(step/of)`; result replaces it; gated calls announced pre-approval; file contents never dumped | selftest `_args_summary`; manual `write …`/zip | Implemented; UNVERIFIED |
| REQ-037 | "Allow device access" must actually do something server-side (and uploads must stay working) | must | grant stored per conversation, enforced by device tools; helper-pairing state surfaced; uploads/downloads unaffected by design + explained | selftest device-gate tests; manual UI | Implemented; UNVERIFIED |
| REQ-038 | Straightforward llama.cpp start: Start button instead of a no-op Reload | must | Start launches, Restart relaunches so settings apply, Stop stops (Hearth-launched only); slow loads show status; external server untouched | selftest manager presence; manual Admin → Model | Implemented; UNVERIFIED |
| REQ-039 | Context usage bar visible to the user | must | tokens used vs window in chat header, thresholds + ≥80 % warning, measured-vs-estimated labelled, persists across reload | selftest `context_usage_payload`; manual | Implemented; UNVERIFIED |
| REQ-040 | Retry / Force respond / talk-as-agent controls | must | Retry re-runs the last user turn without duplicating it or eating newer turns; Force respond is one-shot no-tools; Agent-mode toggle switches tools on/off | selftest `_turn_options`; manual | Implemented; UNVERIFIED |

New endpoints: `POST /api/chat/abort`, `GET /api/chat/running`, `POST /api/chat/retry`,
`GET /api/chat/context-usage`, `POST|GET /api/chat/device-access`, `POST /api/admin/model/start`,
`POST /api/admin/model/stop`. SSE gains the `aborted` event; `tool_call` gains `summary`/`step`/`of`/
`awaiting_approval`; `usage` gains `n_ctx`. New settings: `agent.max_tool_steps`,
`agent.require_device_grant`, `device.helper_url`; new boot vars `HEARTH_STREAM_IDLE_TIMEOUT_S`,
`HEARTH_STREAM_IDLE_GUARD`. New table column: `conversations.device_granted` (idempotent ALTER).
Clarification of REQ-023: the session-2 "mocked" device grant is superseded — the switch is now a real
server-side gate; the *helper* remains unshipped, so device tools still report nothing to talk to.
Non-goals reaffirmed: uploads/downloads are not device access and stay ungated by that switch; no
auto-compaction of over-context conversations yet (display warns only); abort is cooperative and does not
kill a tool mid-execution.


---

## Amendment v1.3 — Linked project folder (design accepted, not yet built)

Added per DEC-049. A user may link **one** real folder to their workspace so they can work on a
project together with Hearth; the agent edits a copy plus a baseline manifest inside the jail, and an
explicitly approved action propagates the diff to the real folder. The feature is **content-agnostic**:
no path-based restrictions, no special treatment of any file or project type. Safety comes from
per-apply approval, conflict detection, backups/revert, and fail-closed refusals — unresolvable
conflicts, encryption at rest, and unsupported operating systems all refuse rather than degrade.

| ID | Requirement | Priority | Verification | Status |
|---|---|---|---|---|
| REQ-041 | Link one real folder per user; agent sees it under a fixed virtual prefix (`project/…`) with existing relative-path tools unchanged | must | selftest: link resolves, `..`/absolute/symlink-escape still refused; unlink restores plain jail | **Design only — not implemented** |
| REQ-042 | Import copies a bounded subtree into the jail and records a baseline manifest (per-file hash, size, mtime) | must | selftest: import → manifest matches disk; edited file detectable by hash | Design only |
| REQ-043 | `sync_preview` lists added/modified/deleted files + conflicts, read-only, never writes outside the jail | must | selftest on a temp dir: preview leaves target byte-identical | Design only |
| REQ-044 | `sync_apply` is gated (`ask`) and refuses any file whose current hash differs from baseline; conflicts are refused, never merged | must | selftest: user-side edit between preview and apply → that file refused, rest applied atomically | Design only |
| REQ-045 | Pre-apply backup of every overwritten target; revert restores it in one action | must | selftest: apply then revert → target identical to pre-apply state | Design only |
| REQ-046 | Apply backend auto-detects per OS (Linux → Windows → Android/helper → stub); unsupported yields a clear refusal, never a partial write | should | selftest with forced backend=`stub`: refusal message + no disk change | Design only |
| REQ-047 | Workspace encryption at rest blocks apply-back with an explanatory refusal (no ciphertext written to the user's folder) | must | selftest with encryption on: apply refuses, target untouched | Design only |
| REQ-048 | Every link/import/preview/apply/revert/refusal is audited with the affected paths | should | selftest: audit rows present for each action | Design only |

New endpoints (planned): `GET|POST /api/project-link` (status, link, unlink), `POST
/api/project-link/import`, `GET /api/project-link/preview`. New tools (planned): `sync_preview`,
`sync_apply`. New settings (planned): `tools.sync_preview[.approval]`, `tools.sync_apply.approval`
(fixed to `ask`, not offered as `auto`), `project.max_import_files`, `project.max_import_bytes`.
New table (planned): `project_links`.

Non-goals for this amendment: no path-based guarding or deny-lists of any kind — the feature must not
behave differently for any file, directory, or project type (see DEC-049 "Rejected: guard-path list");
no real-time mirroring/shadow mode; no automatic conflict merge; no git dependency (an optional
git-backed variant may come later as a complement); no cross-machine or Android write path yet — that
belongs to the helper-protocol design (DEC-049 option A), which the existing `device.helper_url`
scaffolding anticipates; per-file links are explicitly rejected in favour of baseline-relative change
detection.
