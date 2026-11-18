# Hearth — private family LLM harness on llama.cpp

A self-hosted, multi-user chat server that puts a local **llama.cpp** model behind a
secure web UI with per-user workspaces, gated tools (file read/write/upload, sandboxed
command execution, ask-the-user, web search, device access), admin-managed config,
markdown **protocols** and **system prompts**, streaming output with prefill/eval +
tokens/sec telemetry, and mobile+desktop responsive UI.

> Status: integrated prototype / release-candidate. Runs out-of-the-box on a built-in
> **stub model** (no llama.cpp required) so you can evaluate the whole product; point it
> at `llama-server` when ready. See docs/CONTRACT.md for scope and docs/DELIVERY_RECORD.md
> for what is verified vs pending.

> **Continuing this project (or a fresh AI session)?** Start with **docs/CONTINUATION.md** —
> run/test steps, file map, hard-won gotchas, and current status. Then CONTRACT.md + DELIVERY_RECORD.md.

---

## Quick start (stub model — no GPU, no llama.cpp needed)

```bash
# 1. Python 3.10+ recommended
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Fetch the vendored Tailwind build once (no CDN at runtime; see vendor README)
curl -fsSL https://cdn.tailwindcss.com -o hearth/static/vendor/tailwind.js

# 3. Run (binds to localhost only by default)
uvicorn hearth.main:app --host 127.0.0.1 --port 8080
```

On first launch Hearth creates `data/hearth.sqlite3` and prints a one-time **admin
password** in the console (username `admin`). Open http://127.0.0.1:8080 , sign in, then
Admin → Users to add family accounts. Everything works with the stub model so you can try
chat, tools (`list`, `read notes.md`, `run echo hi`), the debug panel, and admin settings.

## Using a real llama.cpp model

1. Build or obtain **llama-server** (CUDA/Metal/CPU as appropriate).
2. Configure via environment (see `.env.example`) — pick one:
   - Point at an already-running server: `HEARTH_LLAMA_BASE_URL=http://127.0.0.1:8081`
   - Or let Hearth launch it: set `HEARTH_LLAMA_SERVER_BIN=/path/to/llama-server` and
     `HEARTH_LLAMA_MODEL_PATH=/path/to/model.gguf`.
3. Admin → Model: set **Backend** = `llama-server`, tune context/RoPE/GPU, click **Reload**.

Context defaults to the full **262144**. If you hit OOM, lower `n_ctx` or use RoPE scaling
(`linear`/`yarn`) — each setting has inline help explaining what it does and its safe range.

### Vision (multimodal / mmproj)
To have Hearth *see* image attachments, pair a vision-capable model with its **mmproj**
projector file (from the same model repo, e.g. `mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf`).
Set it in **Admin → Model → "Vision projector (mmproj) path"** (or `HEARTH_LLAMA_MMPROJ_PATH`
as the env fallback), then **Restart llama-server** — Hearth passes `--mmproj`. The Model panel
shows a **Vision: mmproj set** badge once configured. Leave it blank for text-only models;
image parts are then ignored by the model rather than sent as raw bytes. The projector must
match the main model — a mismatched one fails to load (see `runtime/llama-server.log`).

### Audio: a second model that listens (optional)

Hearth can run a **second llama.cpp model** whose only job is to describe audio, and hand that
description to the chat model as text. Useful for an Omni-class GGUF (Qwen-Omni), which reports
voices, timbre, emotion, music and ambience — not just words — so even a text-only chat model can
reason about a recording.

1. **Admin → Audio** → enable *the audio captioner*.
2. Set **Captioner model (.gguf)** and its matching **mmproj** (same repo as the audio model).
3. Leave **GPU layers** at `0` (CPU only) — the default, chosen for MoE models: they want RAM, and
   partial VRAM residency thrashes while starving the chat model. Set `all` if it fits in VRAM.
4. Keep **port 8091** distinct from the chat model's, then **Start captioner** (weights can take
   minutes; log at `runtime/captioner.log`).
5. **Test with a tone** captions a generated 2-second beep to verify ffmpeg → request → audio encoder
   → description in one click. If it describes speech over a beep, the model or prompt is wrong.

Audio attachments are then described automatically as you send them (toggle in Admin → Audio), and the
assistant can call `describe_audio` on any workspace file itself. Long recordings are split into timed
chunks (`captioner.chunk_seconds`, needs `ffmpeg`) so it can say "at 1:30 she laughs". Both backends —
`server` (weights stay loaded) and `cli` (`llama-mtmd-cli`, frees memory when idle) — are selectable;
`server` falls back to `cli` mid-caption if its process dies.

**Admin → Model → Loaded models** lists the chat model *and* the captioner with real running/configured
state, and each can be started or stopped without disturbing the other. Env fallbacks:
`HEARTH_CAPTIONER_MODEL_PATH`, `_MMPROJ_PATH`, `_SERVER_BIN`, `_CLI_BIN`, `_BASE_URL` (an externally
run captioner server Hearth will use but never launch or kill). See docs/FEATURES.md and DEC-056.

## Sandboxed command execution (gated)

Off by default. Enable in Admin → Tools → **Command execution**. Hearth auto-detects a
sandbox backend at runtime: **bubblewrap (`bwrap`)** → **Docker** → **stub(refuse)**. If no
sandbox is available it *refuses* to run commands rather than running them unsandboxed.
Every exec still requires the user to approve that specific command in the chat UI.

```bash
# Debian/Ubuntu:  sudo apt install bubblewrap
# or ensure Docker is available for the docker backend
```

## Access from another device on your LAN

Default is `127.0.0.1` (this machine only). To let family devices reach it, bind all
interfaces and open the port on the firewall:

```bash
HEARTH_HOST=0.0.0.0 python -m hearth.main
# Debian/Ubuntu + ufw:   sudo ufw allow from 192.168.0.0/24 to any port 8080 proto tcp
```

Then family members open `http://<server-lan-ip>:8080` and sign in with the accounts you
create in **Admin → Users** (per-user workspaces keep everyone's files apart; logins are
rate-limited, and per-user TOTP 2FA can be enabled from the account menu).

Two things this makes real, so do them deliberately:

- **Traffic is plain HTTP.** Passwords go over the network unencrypted. That is usually
  acceptable on a trusted home LAN; it is not acceptable beyond it. For anything past the
  router, prefer Tailscale (below) plus HTTPS and `HEARTH_COOKIE_SECURE=true`.
- **The CSRF cookie must be sent from another origin.** Hearth's session cookie is
  `SameSite=Strict`, which also blocks a *legit* request when you reach the server by
  hostname/IP rather than the address that set the cookie. If a family member can load
  the page but every send fails, start with `HEARTH_COOKIE_SAMESITE=lax` (still CSRF-safe;
  it relaxes only cross-site *subrequests*). Keep `Strict` on localhost.

## Access from outside the LAN (recommended: Tailscale)

Hearth never opens a *public* port by design. Join each family device to a **Tailscale**
tailnet and reach the server at its tailnet IP; front with HTTPS.
Optionally enable Tailscale identity SSO (`HEARTH_TAILSCALE_SSO_ENABLED=1`, with the
server's own/tailnet IPs in `HEARTH_TRUSTED_PROXY_IPS`) so logged-in tailnet users map to
local accounts. Per-user **TOTP 2FA** can be enabled from the account menu. See
docs/SECURITY.md for full hardening guidance (TLS, cookies, rate limits, backups).

## Run tests

```bash
python scripts/selftest.py                 # stdlib core checks
HEARTH_RUN_HTTP=1 python scripts/selftest.py   # + HTTP integration (needs deps)
```

## Project layout

```
hearth/            application package
  main.py          FastAPI app factory, bootstrap admin, page routes, security headers/CSP
  config.py        boot/env settings    runtime_settings.py  admin-editable settings+help
  db.py models.py  SQLite (SQLAlchemy)   security.py  scrypt/tokens/TOTP/CSRF
  deps.py          authn/authz + CSRF     ratelimit.py  lockout
  workspaces.py    per-user path jail    sandbox.py    bwrap/docker/stub exec
  artifacts.py     durable per-user files (produced/uploaded) + ownership-scoped download
  i18n.py          German/English UI strings + per-user language preference
  orchestrator.py  per-turn agent loop   prompt.py     system prompt assembly
  manager.py       llama-server lifecycle provider selection (llm/)
  captioner.py     SECOND model: audio description (own server/port/log; see DEC-056)
  tools/           fs(read/write/list/upload), exec, ask_user, web_search, device
  routes_auth/chat/admin.py               templates/ static/  UI (Jinja2 + vendored Tailwind)
docs/              CONTRACT DECISIONS ARCHITECTURE SECURITY OPERATIONS FEATURES DELIVERY_RECORD
scripts/selftest.py
```

See docs/ARCHITECTURE.md for the system map and trust boundaries, docs/FEATURES.md for the
user guide (uploads, artifacts, device access, language), docs/OPERATIONS.md for runbooks.

## Notes & limitations

- Single-process design (SQLite + in-memory rate limiting). Fine for family scale; a reverse proxy
  can add TLS. For many concurrent users run behind `uvicorn --workers` with an external DB and move
  rate-limiting to Redis (follow-ups listed in the delivery record). Downloads are durable via the
  artifacts table (no longer tied to in-memory tokens).
- **Device tools are stubbed behind a documented helper protocol until a device helper is paired.** The
  "Allow device access" button is a real per-conversation server-side gate (the tools refuse without it),
  but with no helper daemon shipped there is nothing for them to talk to — the UI says so. Single-file
  upload/download is the working file path and never needs that switch.
- One agent turn per conversation at a time (in-process registry); aborts are cooperative and do not kill
  a tool mid-call. Context bar warns on overflow but nothing auto-compacts yet.
- Web tools (`web_search` + `web_fetch`) follow an admin-set **web-access mode**: DuckDuckGo HTML over
  plain HTTP (default, can fail gracefully — it's user-triggered only), the same via a local Tor SOCKS5
  proxy, or the operator's real Chrome via Selenium (`pip install selenium`) for JS-heavy/login pages.
  Results render as clickable source links in the chat so answers can be verified. See Admin → Tools → Web.
