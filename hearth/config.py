"""Static process configuration (env-driven) via pydantic-settings.

This is the *boot* config — secrets, bind address, cookie signing key, DB path.
Everything an admin can change at runtime (model settings, tool enablement,
protocol selection) lives in the DB-backed RuntimeSettings store (see
`runtime_settings.py`) and is edited through the admin UI. Keep these separate:
boot config is read-only for the app; runtime config is user-editable.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import secrets as _secrets


class BootSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HEARTH_", env_file=".env", extra="ignore")

    # --- HTTP server binding (default localhost; front with Tailscale/TLS) ---
    host: str = "127.0.0.1"
    port: int = 8080

    # --- Cookie / token signing ---
    # Generate one for production: python -c "import secrets;print(secrets.token_hex(32))"
    secret_key: str = Field(default_factory=lambda: _secrets.token_hex(32))
    cookie_name: str = "hearth_session"
    cookie_secure: bool = False          # set True when serving over HTTPS
    # "strict" is the safest CSRF posture and the default. It also blocks a legitimate
    # request when the same app is reached from a different origin than the one that set
    # the cookie — e.g. localhost on the server itself + its LAN IP for family devices.
    # For that (LAN exposure) use "lax": still blocks cross-site subrequests/POSTs from
    # other sites, so CSRF stays defended. Anything else falls back to "strict".
    cookie_samesite: str = "strict"
    session_ttl_hours: int = 12
    csrf_cookie_name: str = "hearth_csrf"

    # --- Database ---
    db_url: str | None = None            # default derived from paths.DB_PATH

    # --- First-run admin bootstrap (created if no users exist) ---
    bootstrap_admin_user: str = "admin"
    bootstrap_admin_password: str = ""   # if empty, a random one is printed once

    # --- llama.cpp / llama-server ---
    use_stub_llm: bool = True            # if False and binary/base_url present -> real
    llama_server_bin: str = ""           # path to llama-server (empty = not launched)
    llama_base_url: str = ""             # existing server base URL (skip launching)
    llama_model_path: str = ""
    # Vision projector file (mmproj-*.gguf) for multimodal models. Optional: when
    # set, Hearth passes --mmproj so the server can "see" image attachments.
    llama_mmproj_path: str = ""
    llama_port: int = 8081               # port for the llama-server Hearth launches
    llama_jinja: bool = True             # pass --jinja: proper chat templates incl. tool calling
                                         # (Hermes/Llama-3.1/Qwen function-calling needs this)
    llama_extra_args: str = ""           # raw extra CLI args appended verbatim, e.g.
                                         # "--chat-template some.jinja --top-k 40"
    n_ctx: int = 262144                  # default full context (DEC-008)

    # --- Tailscale / trusted-header SSO (disabled by default) ---
    tailscale_sso_enabled: bool = False
    trusted_proxy_ips: str = ""          # comma list; only these may set identity hdrs

    # --- Web search ---
    web_search_enabled: bool = True

    # --- Workspace encryption at rest (opt-in; see hearth/crypto.py) ---
    # Files are encrypted with XChaCha20-Poly1305 on write and decrypted in memory
    # on read. Protects disk/backups/device loss; NOT protection against someone who
    # already owns the running server+RAM. Exec sandbox sees ciphertext (documented).
    workspace_encryption: bool = False
    workspace_enc_key: str = ""          # 64-hex chars (32 bytes) OR use the passphrase below
    workspace_enc_pass: str = ""         # if set, key = scrypt(passphrase, salt=secret_key)

    # --- Rate limiting / lockout ---
    login_max_attempts: int = 8
    login_lockout_seconds: int = 900

    # --- Streaming safety valve ---
    # If a provider stream goes this long without yielding ANY event, the turn is
    # ended with an error instead of hanging forever (Abort always works too).
    stream_idle_timeout_s: int = 300

    # --- Python compatibility ---
    # The idle-timeout guard uses asyncio.timeout (Python 3.11+). On older runtimes
    # it is skipped automatically — Abort still works, only the automatic hang
    # guard is unavailable. Set False to disable it explicitly on 3.11+.
    stream_idle_guard: bool = True

    @property
    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        from . import paths
        return f"sqlite:///{paths.DB_PATH}"

    @property
    def normalized_cookie_samesite(self) -> str:
        """SameSite value Starlette will accept, or 'strict' for anything else.

        LAN exposure (HEARTH_HOST=0.0.0.0) is a supported operator choice and "lax" is the
        documented relaxation for it. An unrecognised value must never silently become
        "none": SameSite=None without Secure is rejected by browsers *and* is no CSRF
        protection at all, so a typo in .env degrading to None would be both broken and
        unsafe. Strict is the safe floor.
        """
        v = str(self.cookie_samesite or "").strip().lower()
        return v if v in ("strict", "lax", "none") else "strict"


@lru_cache
def get_boot_settings() -> BootSettings:
    return BootSettings()
