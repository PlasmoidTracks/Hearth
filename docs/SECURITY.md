# Security

Hearth is built for a family LAN that may be reached from outside via an authenticated
tunnel. It is **secure-by-default** but not a hard multi-tenant boundary. Read this before
exposing it beyond localhost.

## Identity & access control
- **Local accounts only.** No public sign-up; the admin creates users. First run bootstraps
  one admin (random password printed once). Foreign visitors see only the login page.
- **Password hashing**: `hashlib.scrypt` (N=16384, r=8, p=1, explicit maxmem) or argon2 if installed. Never
  stored or logged in plaintext. Self-describing encodings allow future migration.
- **Sessions**: HMAC-signed cookie carrying a DB session id; server-side revocation on logout
  and "log out everywhere". Cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` when
  `HEARTH_COOKIE_SECURE=true`.
- **TOTP 2FA** (RFC6238): optional per-user; required at login once enrolled. Enroll from the
  account menu (Admin can also manage users).
- **Tailscale SSO** (optional, off by default): maps `Tailscale-User-Login`/`Remote-User` to a
  local account ONLY when the request comes from a trusted proxy IP (`HEARTH_TRUSTED_PROXY_IPS`).

## Content Security Policy & self-hosted assets
- **No CDN at runtime.** Tailwind is vendored locally (`hearth/static/vendor/tailwind.js`;
  fetch once per that dir's README — it isn't committed). No page references any third-party
  origin, so a family device loading the UI makes zero external requests. HTMX is unused.
- **Strict CSP** on every response (see `main._add_security_headers`): `default-src 'self'`,
  `script-src 'self'` (no inline scripts — all JS is in external files), `frame-ancestors 'none'`
  (anti-clickjacking), `object-src 'none'`, `form-action 'self'`, `base-uri 'self'`,
  `connect-src 'self'`. Styles allow `'unsafe-inline'` because Tailwind's runtime JIT injects
  styles; this is the one deliberate concession and is low-risk. Self-hosting a static Tailwind
  CSS build would let us drop it (follow-up).
- Also set: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
  `X-Frame-Options: DENY`, and HSTS (`Strict-Transport-Security`) when `HEARTH_COOKIE_SECURE=true`.

## Transport & edge (operator responsibility)
Hearth binds `127.0.0.1` and does not terminate TLS itself. Put it behind:
- **Tailscale** (recommended): reach via tailnet IP; no public port; optionally enable HTTPS on
  the tailnet (`tailscale serve`) and set `HEARTH_COOKIE_SECURE=true`.
- or a **TLS reverse proxy** (Caddy/nginx) with valid certs + HSTS, then set
  `HEARTH_COOKIE_SECURE=true` and add the proxy IP to `HEARTH_TRUSTED_PROXY_IPS` so
  `X-Forwarded-For` is trusted for rate limiting.

### LAN exposure (`HEARTH_HOST=0.0.0.0`) — accepted risk, stated plainly
Binding all interfaces to let family devices in on a home network is a supported operator
choice, and startup prints a warning when it happens over plain HTTP. What changes:
- Credentials and chat traffic are unencrypted on the LAN (anyone who can sniff the segment).
  Fine for a trusted home network; not fine beyond it — use Tailscale/TLS past the router.
- The login rate limiter is per `(ip, username)` and in-process, so one shared NAT address
  concentrates attempts; lockout still applies but affects that household IP.
- Anyone on the LAN can *reach* the login page (not the accounts): keep passwords real, add
  per-user TOTP, and don't rely on "they can't see the port" as a control.
- **Cookie policy:** `SameSite=Strict` can block legitimate sends when the same app is reached
  from several origins (localhost + LAN IP). `HEARTH_COOKIE_SAMESITE=lax` is the supported
  relaxation for that case — it still blocks cross-site *subrequests*, and remains the default
  on localhost.

Never port-forward Hearth directly to the public internet without TLS + a WAF/Zero-Trust layer.

## CSRF & request safety
- All mutating endpoints require a double-submit CSRF token (header matches csrf cookie); GETs
  are exempt. Combined with `SameSite=Strict`, cross-site form posts are blocked.
- Every data query is scoped to the authenticated user; admin endpoints additionally require
  `is_admin`.

## Brute-force protection
Per `(ip, username)` failed-login counter with lockout (`HEARTH_LOGIN_MAX_ATTEMPTS` /
`..._LOCKOUT_SECONDS`). Failures and lockouts are audit-logged. In-memory (single process).

## Workspace isolation
All file access goes through `workspaces.py`, which canonicalizes the path and verifies it stays
under the user's root, rejecting absolute paths, `..` traversal, and symlinks pointing outside.
Unit-tested in `scripts/selftest.py`. Downloads use short-lived, process-scoped tokens bound to
a concrete file path.

**The one deliberate exception:** `GET /api/admin/browse-path` (`hearth/pathbrowse.py`) is *not*
jailed — it lists directories anywhere on the server so an admin can point Hearth at a model or
binary outside any jail. It is `require_admin`, read-only (no write/move/delete counterpart exists
to reach), runs off the event loop, caps and labels its listing, refuses relative paths rather than
guessing a base, and audits every call as `path_browse`. The premise: an admin who can enable
Command execution already has this filesystem, so a path allowlist would break real setups without
adding protection. **Never hand out the admin role to someone you would not give a shell on that
machine** — that is the true blast radius, browsing or no browsing. User-facing file access remains
fully jailed and unchanged.

## Command execution (defense in depth)
Three independent controls must all allow a command:
1. Admin enablement (`tools.exec`, default off).
2. Per-call explicit user approval (gated tool; denial is recorded and fed back to the model).
3. Sandbox isolation — bwrap namespaces (`--unshare-user/pid/net/ipc/uts`), read-only system,
   writable jail only, dropped capabilities, `no-new-privileges`, 60s timeout, output caps; or
   Docker `--network none --cap-drop ALL --read-only`. If neither exists it **refuses** to run.

Residual risk: bwrap/Docker are strong but not a hard boundary against a determined local
attacker with kernel privileges. Run on a dedicated machine/VM and keep the model host patched.

## Device access
The `device_*` tools (folder listing, screenshot) are gated by **two** independent conditions, enforced
in the tool (`tools/device_tools._gate`) rather than trusting UI state:
1. A device helper must be paired (`device.helper_url` admin setting, or `HEARTH_DEVICE_HELPER_URL`).
   No helper ships yet, so today these tools report that nothing is paired — no data leaves a device.
2. The user must have granted device access **for that conversation** — the chat-header button, stored as
   `conversations.device_granted` and injected into tool context server-side (`_device.granted`). Clients
   cannot self-grant: the value is only ever read from our DB. An admin may drop this requirement
   (Admin → Agent → Require device-access grant). Screenshots additionally stay approval-gated per call.

**Uploads and downloads are not device access.** They transfer files the user explicitly chose, initiated
by the user, into/out of that user's own jail; they are unaffected by either switch above and are governed
by the usual size/ownership/jail rules.

## Turn control (abort / retry / tool budget) — security notes
- **Abort is owner-scoped**: `/api/chat/abort` only cancels a turn whose registered conversation belongs to
  the caller (404-free no-op otherwise), and `GET /api/chat/running` reports only the caller's own state.
- **Retry deletes data** — but only assistant/tool rows of the caller's own last turn, after an ownership
  check on the conversation. It cannot touch another user's chat or a newer turn, and refuses while a turn
  is running (409). No undo: treat it as destructive-but-bounded.
- **Client-supplied `no_tools` can only reduce capability.** It sends an empty tool-spec list for one turn;
  it cannot enable a disabled tool, bypass an approval gate, or grant device access. `max_tokens` is clamped
  to `[16, model.max_tokens]`, so a client cannot raise the admin cap.
- **Tool-step budget** (`agent.max_tool_steps`) is a resource/abuse guard: it bounds how many model
  round-trips and tool invocations one message can trigger (relevant on a shared GPU box).
- Abort state lives in process memory; after a restart nothing is running, and cross-process abort is a
  documented scaling follow-up (see OPERATIONS.md).

## Data handling & privacy
- Personal data (conversations, workspace files) stays on your hardware; nothing is sent out
  except user-initiated web searches/fetches and (if configured) outbound model calls — inference
  itself is local. Web traffic is policy-gated: mode `off` disables it entirely; `tor` routes the
  HTTP backends through a local Tor SOCKS5 proxy (remote DNS, Tor Browser UA); fetched URLs, search
  queries and page sources are persisted in chat history (tool rows) so family members can check
  sources later — treat conversation exports accordingly.
- **SSRF guard:** web tools refuse loopback/link-local/private targets (incl. 169.254.169.254) by
  default so a prompt-injected page can't make Hearth probe your LAN. It is best-effort (public
  hostnames resolving to private IPs are not resolved pre-flight) and intentionally not applied in
  `open_browser` mode, where the operator's own browser is the trust boundary. Admin override:
  `web.http_allow_private`.
- **`open_browser` web mode** hands the model a real Chrome carrying the *operator's* logged-in
  cookies (full JS). Powerful for SPAs/login-gated pages; dangerous on shared installs — keep the
  per-tool approval at `ask`, set `web.block_domains` (e.g. banking), and prefer
  `search_and_fetch_safe` otherwise. Selenium is an optional dependency (`pip install selenium`).
- Audit log records security events (login/lockout, exec + command text, permission decisions,
  admin changes). Command text is retained intentionally for accountability; do not treat audit
  output as secret-free and restrict access to it.
- Secrets: `HEARTH_SECRET_KEY` signs sessions — keep it private and stable (rotating it logs
  everyone out). Never commit `.env`.

## Known limitations / residual risks
- Single-process rate limiting + download-token store → not shared across multiple workers; if
  you scale to several processes, move these to Redis or sticky sessions.
- SQLite is robust for family scale but single-writer; heavy concurrent writes serialize.
- Device tools are stubs until a real device helper is paired (no data leaves via them today), but the
  grant + URL plumbing around them is real and enforced server-side.
- Context-window display is derived from persisted usage (previous turn) or a ~4 chars/token estimate; it
  informs, it does not enforce. Nothing yet auto-compacts a conversation that outgrows `n_ctx`.
- CSP allows `style-src 'unsafe-inline'` because Tailwind's runtime JIT injects styles; the
  one concession. Dropping it requires a static-CSS Tailwind build (follow-up). Scripts remain
  strict (`'self'`, no inline). Tailwind dev-build JS is heavier than a static bundle.
