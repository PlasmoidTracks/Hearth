"""Admin-editable runtime settings (DB-backed) with typed schema + help text.

The admin UI renders each SETTING_SPEC entry with its label, description, type,
and safe range — satisfying the "transparent model settings with good info behind
what values do" requirement. Values persist in the `settings` table; defaults
come from here. Changing model-affecting fields requires a model reload
(manager.reload()) to take effect on llama-server.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field

from .db import session_scope


@dataclass
class SettingSpec:
    key: str
    label: str
    type: str            # int | float | bool | str | choice
    default: object
    description: str
    choices: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    group: str = "general"
    requires_reload: bool = False


# ---- Model / llama-server settings (DEC-008) --------------------------------
MODEL_SPECS: list[SettingSpec] = [
    SettingSpec("model.backend", "Backend", "choice", "stub",
                "Which LLM provider to use. 'stub' runs a built-in fake model so the app "
                "works with no llama.cpp. 'llama-server' uses a real llama.cpp server.",
                choices=["stub", "llama-server"], group="model", requires_reload=True),
    SettingSpec("model.n_ctx", "Context length (n_ctx)", "int", 262144,
                "Total context window in tokens (prompt + generation). Default is the full "
                "262144. Higher = more memory. If you OOM, lower this or use RoPE scaling.",
                min=512, max=1_000_000, group="model", requires_reload=True),
    SettingSpec("model.rope_scaling", "RoPE scaling type", "choice", "none",
                "Extends effective context beyond the model's trained length. 'none' uses native; "
                "'linear' stretches positions (cheap, mild quality loss); 'yarn' is higher quality "
                "for large extensions. Requires a compatible model.",
                choices=["none", "linear", "yarn"], group="model", requires_reload=True),
    SettingSpec("model.rope_freq_base", "RoPE frequency base", "float", 10000.0,
                "Base for rotary embeddings. With yarn/linear extension you often raise this "
                "(e.g. 500000) to preserve resolution at long range. Leave at 10000 unless following "
                "a specific model card.",
                min=100.0, max=10_000_000.0, group="model", requires_reload=True),
    SettingSpec("model.rope_freq_scale", "RoPE frequency scale", "float", 1.0,
                "Scaling factor for position interpolation. <1 stretches positions to cover more "
                "context (used with linear/yarn). Typical extension values ~0.25–0.75.",
                min=0.01, max=4.0, group="model", requires_reload=True),
    SettingSpec("model.n_gpu_layers", "GPU layers (n_gpu_layers)", "choice", "auto",
                "How many transformer layers to offload to VRAM. 'auto' leaves the flag unset so "
                "llama.cpp offloads as many layers as fit (recommended; matches a plain llama-server "
                "run). 'all' forces every layer onto the GPU (-1) — fastest if it fits, but fails or "
                "spills to CPU on a model larger than VRAM. 0 = CPU only. Use a specific number to cap "
                "it manually when a big MoE model won't fit. Only applies to a GPU-built llama.cpp.",
                choices=["auto", "all", "0"], group="model", requires_reload=True),
    SettingSpec("model.flash_attn", "Flash attention", "choice", "auto",
                "Flash-attention kernels for speed/memory at long context. 'auto' lets llama.cpp choose; "
                "'on' forces it (recommended on modern GPUs); 'off' disables.",
                choices=["auto", "on", "off"], group="model", requires_reload=True),
    SettingSpec("model.n_parallel", "Parallel slots", "int", 2,
                "Number of concurrent generation slots in llama-server (how many chats can decode at "
                "once on one model instance). More slots = more RAM for KV cache.",
                min=1, max=16, group="model", requires_reload=True),
    SettingSpec("model.include_usage", "Ask the server for token usage", "bool", True,
                "Send stream_options.include_usage with every request so the server reports real "
                "token counts. This is what makes the context bar show measured tokens instead of a "
                "characters/4 guess (the guess misses the system prompt, tool calls and tool results). "
                "Turn OFF only if your server rejects the field — then the bar falls back to estimates.",
                group="model"),

    # ---- Server / model paths (the UI-driven loading controls) --------------
    # Empty means "fall back to the HEARTH_LLAMA_SERVER_BIN / _MODEL_PATH env var".
    SettingSpec("model.server_bin", "llama-server binary path", "str", "",
                "Absolute path to your llama-server executable, e.g. "
                "/home/you/llama.cpp/build/bin/llama-server. Leave blank to use the "
                "HEARTH_LLAMA_SERVER_BIN environment variable instead. Hearth launches this only "
                "when you press Start (Admin → Model) or send a chat message with Backend set to "
                "'llama-server' — opening the chat page never starts a model.",
                group="model", requires_reload=True),
    SettingSpec("model.model_path", "Model (.gguf) path", "str", "",
                "Absolute path to the model file to load, e.g. "
                "/home/you/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf. Leave blank to use HEARTH_LLAMA_MODEL_PATH. "
                "Pick a model with native tool/function calling (Qwen2.5, Llama-3.1, Hermes) so the agent "
                "tools work.",
                group="model", requires_reload=True),
    SettingSpec("model.mmproj_path", "Vision projector (mmproj) path", "str", "",
                "Optional. Absolute path to the mmproj vision projector that pairs with the model above, "
                "e.g. /home/you/models/mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf. When set, Hearth passes "
                "--mmproj so llama-server can see image attachments (multimodal). Leave blank for text-only "
                "models — image parts are then ignored by the model. Requires a vision-capable model + its "
                "matching mmproj file from the same repo; a mismatched projector fails to load.",
                group="model", requires_reload=True),

    # ---- Performance tuning -------------------------------------------------
    SettingSpec("model.threads", "CPU threads (-t)", "int", -1,
                "Threads used during generation. -1 = auto (let llama.cpp pick from your CPU). Lower it to "
                "leave headroom for other work; raise only if you know your box benefits.",
                min=-1, max=256, group="model", requires_reload=True),
    SettingSpec("model.batch_size", "Batch size (-b)", "int", 2048,
                "Logical max batch size — bigger processes prompts faster but uses more VRAM. Common to "
                "raise to 4096–8192 on a roomy GPU for snappier first-token on long chats.",
                min=8, max=65536, group="model", requires_reload=True),
    SettingSpec("model.ubatch_size", "Physical ubatch (-ub)", "int", 512,
                "Physical micro-batch size used with -b. Usually leave at 512; raise alongside -b if you "
                "tuned batch for throughput.",
                min=8, max=65536, group="model", requires_reload=True),

    # ---- Memory / fit helpers ----------------------------------------------
    SettingSpec("model.kv_cache_type", "KV cache quant (-ctk/-ctv)", "choice", "f16",
                "Quantize the attention KV cache to fit large contexts in VRAM. f16 = default (best "
                "quality). q8_0 ≈ half the memory with negligible loss — great for big contexts. q4_0 saves "
                "more but can degrade quality.",
                choices=["f16", "q8_0", "q4_0"], group="model", requires_reload=True),
    SettingSpec("model.device", "Device(s) (--device)", "str", "",
                "Comma-separated device names to offload to (see llama-server --list-devices). Leave blank "
                "for auto. Use 'none' to force CPU-only, or name a specific GPU on multi-GPU boxes.",
                group="model", requires_reload=True),
    SettingSpec("model.load_mode", "Load mode (-lm)", "choice", "auto",
                "How weights are loaded. 'auto' = mmap unless a device can't. 'mmap' memory-maps the file; "
                "'mlock' pins it in RAM (avoids page-out stalls); 'none' plain read.",
                choices=["auto", "mmap", "mlock", "none"], group="model", requires_reload=True),

    # ---- Reasoning / thinking (DeepSeek/Qwen-R style models) ----------------
    SettingSpec("model.reasoning", "Reasoning / thinking", "choice", "auto",
                "Enable thinking/reasoning tokens if the model supports them. 'auto' detects from the chat "
                "template; 'on'/'off' force it. Affects how output is parsed into the answer.",
                choices=["auto", "on", "off"], group="model", requires_reload=True),
    # NOTE: temperature/top_p are generation-time settings sent per request (see
    # LlamaServerProvider.stream) — they take effect immediately and deliberately do
    # NOT carry requires_reload, so the ⟳ hint never lies about needing a restart.
    SettingSpec("model.temperature", "Temperature", "float", 0.7,
                "Sampling randomness. 0.0 = deterministic/greedy; ~0.7 balanced; >1 more creative.",
                min=0.0, max=2.0, group="generation"),
    SettingSpec("model.top_p", "Top-p (nucleus)", "float", 0.95,
                "Nucleus sampling cutoff: sample only from the smallest set of tokens with cumulative "
                "probability ≥ top_p.", min=0.0, max=1.0, group="generation"),
    SettingSpec("model.max_tokens", "Max new tokens / reply", "int", 2048,
                "Cap on generated tokens per assistant message (per turn).", min=1, max=32768,
                group="generation"),
    # Sent to llama-server as `repetition_penalty` on every completion request. It is a
    # generation-time sampling parameter (like temperature/top_p), so it applies without
    # reloading the model — and it only affects NEW tokens: it cannot fix a model that
    # repeats whole tool calls across rounds, which the agent loop guards separately.
    SettingSpec("model.repetition_penalty", "Repetition penalty", "float", 1.0,
                "Penalty for re-using tokens the model has already produced in this "
                "completion. 1.0 = off (recommended default). Small models that fall into "
                "a loop — repeating the same sentence, or the same tool call over and over "
                "— usually calm down at 1.05–1.15. Above ~1.3 quality degrades and output "
                "can get garbled or evasive, so raise it in small steps. Applies to new "
                "tokens only; it does not change what the model does between tool rounds.",
                min=1.0, max=2.0, group="generation"),
]

# ---- Agent loop behaviour ---------------------------------------------------
AGENT_SPECS: list[SettingSpec] = [
    SettingSpec("agent.max_tool_steps", "Max tool steps per turn", "int", 6,
                "How many times the model may call tools in ONE turn before it must answer. "
                "Raise it for multi-step jobs (write several files, search then read them); lower "
                "it to keep a chatty model from burning time/tokens on tool loops. Each step is one "
                "model round-trip plus the tool calls it requested.",
                min=1, max=50, group="agent"),
    SettingSpec("agent.require_device_grant", "Require device-access grant", "bool", True,
                "When on, the device tools (folder listing, screenshot) refuse to run until the "
                "user has pressed 'Allow device access' in this chat. The grant is per conversation "
                "and lasts until it is revoked or the chat is deleted. User-initiated uploads/downloads "
                "are NOT device access and always work.",
                group="agent"),
]

# ---- Tool enablement --------------------------------------------------------
# Granular model: each tool has `tools.<name>` (bool) and `tools.<name>.approval`
# ("auto" = run silently, "ask" = require per-call approval). Legacy keys
# (tools.fs_read / tools.fs_write / tools.device) remain as fallbacks for older
# stored settings — see hearth/tools/__init__.py:enabled_tools().
_TOOL_LABELS = {
    "fs_read": "Filesystem read", "fs_list": "Filesystem list", "fs_search": "Filesystem search",
    "fs_write": "Filesystem write/overwrite", "fs_append": "Filesystem append",
    "fs_edit": "Filesystem edit (find/replace)", "fs_delete": "Filesystem delete",
    "fs_move": "Filesystem move/rename", "zip_pack": "Zip bundling",
    "upload": "Offer file for download", "web_search": "Web search", "web_fetch": "Web fetch",
    "exec": "Command execution", "datetime": "Date & time",
    "device_list": "Device folder listing", "device_screenshot": "Device screenshot",
}

def _tool(name: str, desc: str, default: bool = True, group: str = "tools"):
    return SettingSpec(f"tools.{name}", f"{_TOOL_LABELS[name]}: enable", "bool", default, desc, group=group)


def _approval(name: str, default: str, extra: str = "", group: str = "tools"):
    return SettingSpec(f"tools.{name}.approval", f"{_TOOL_LABELS[name]}: approval", "choice", default,
                       "How each call is approved. 'auto' runs silently; 'ask' shows an approve/deny "
                       "card in the chat first." + extra,
                       choices=["auto", "ask"], group=group)


TOOL_SPECS: list[SettingSpec] = [
    # --- filesystem: read side ---
    _tool("fs_read",
          "Let the assistant read text files (whole file or line ranges) inside a user's own workspace.",
          group="tools-fs"),
    _tool("fs_list",
          "Let the assistant list directories in a user's workspace.", group="tools-fs"),
    _tool("fs_search",
          "Regex/grep across file contents in the workspace (still jailed; read-only).", group="tools-fs"),
    # --- filesystem: write side (each separately controllable) ---
    _tool("fs_write",
          "Create or fully overwrite files in the user's workspace. Destructive vs existing content.",
          group="tools-fs"),
    _tool("fs_append",
          "Append text to a file without touching its existing content.", group="tools-fs"),
    _tool("fs_edit",
          "Exact search-and-replace inside one file; unique matches required unless replace_all.",
          group="tools-fs"),
    _tool("fs_delete",
          "Delete files from the user's workspace. Irreversible — approval 'ask' by default.",
          group="tools-fs"),
    _tool("fs_move",
          "Rename or move files within the workspace jail.", group="tools-fs"),
    _tool("zip_pack",
          "Bundle several workspace files into one downloadable .zip artifact.", group="tools-fs"),
    _tool("upload",
          "Hand an existing workspace file to the user as a download (does not modify files).",
          group="tools-fs"),
    # --- web ---
    _tool("web_search",
          "Search the web and return titles/URLs/snippets. The backend follows the "
          "Web access mode below.", group="tools-web"),
    _tool("web_fetch",
          "Fetch one URL and return its readable text (stripped markup, truncated). "
          "The backend follows the Web access mode below.", group="tools-web"),
    SettingSpec("web.mode", "Web access mode", "choice", "search_and_fetch_safe",
                "How web tools reach the internet. 'off' disables them entirely. "
                "'search_and_fetch_safe' uses DuckDuckGo's HTML endpoint plus plain HTTP fetch — "
                "no JavaScript, so JS-heavy pages return little. 'tor' does the same over a local "
                "Tor SOCKS5 proxy (needs pip install 'httpx[socks]' and a running Tor). "
                "'open_browser' drives the operator's REAL Chrome via Selenium (pip install selenium): "
                "full JavaScript plus logged-in cookies — powerful, but the model could read any site "
                "the browser session can, so keep it off on shared installs.",
                choices=["off", "search_and_fetch_safe", "tor", "open_browser"], group="tools-web"),
    SettingSpec("tools.web_search.engine", "Default search engine", "choice", "duckduckgo",
                "Engine used when the model doesn't name one. 'google' only takes effect in "
                "'open_browser' mode; every other mode searches DuckDuckGo.",
                choices=["duckduckgo", "google"], group="tools-web"),
    SettingSpec("web.headless", "Browser mode: headless", "bool", True,
                "For 'open_browser': run Chrome without a visible window. Turn OFF to watch the "
                "browser work on this machine (useful for debugging logins/CAPTCHAs).",
                group="tools-web"),
    SettingSpec("web.tor_socks_host", "Tor SOCKS host", "str", "127.0.0.1",
                "For 'tor' mode: address of the local Tor SOCKS5 proxy. Leave at 127.0.0.1 unless "
                "Tor runs elsewhere.", group="tools-web"),
    SettingSpec("web.tor_socks_port", "Tor SOCKS port", "int", 9050,
                "For 'tor' mode: 9050 is the standalone tor daemon; Tor Browser's own proxy is 9150.",
                min=1, max=65535, group="tools-web"),
    SettingSpec("web.allow_domains", "Allow-listed domains", "str", "",
                "Optional comma-separated list (example.org, wiki). When non-empty, web_fetch may only "
                "read these domains and their subdomains. Search itself still needs DuckDuckGo/Google.",
                group="tools-web"),
    SettingSpec("web.http_allow_private", "Allow private/internal addresses", "bool", False,
                "By default web tools refuse loopback, link-local (incl. the 169.254.169.254 cloud "
                "metadata address), and private network ranges — this blocks SSRF against Hearth itself "
                "and neighbours on your LAN. Only enable if you deliberately want the assistant to reach "
                "internal services (e.g. a home wiki by IP). 'open_browser' mode is not covered by this "
                "check, since there the operator's own browser is the trust boundary.",
                group="tools-web"),
    SettingSpec("web.block_domains", "Block-listed domains", "str", "",
                "Comma-separated domains that web tools refuse in EVERY mode (subdomains included). "
                "Useful with open_browser, e.g. banking sites.", group="tools-web"),
    SettingSpec("web.max_text_chars", "Fetch: max characters to the model", "int", 8000,
                "Cap on how much extracted page text a single web_fetch hands the model. Raise for "
                "long articles (costs context tokens); lower to keep turns cheap.",
                min=1000, max=64000, group="tools-web"),
    SettingSpec("web.max_links", "Fetch: max links listed", "int", 30,
                "How many of the page's links are appended after the text (and shown as source links).",
                min=0, max=200, group="tools-web"),
    # --- system ---
    _tool("exec",
          "Enable the sandboxed shell tool. Off by default; approval 'ask' by default.",
          default=False, group="tools-sys"),
    _approval("exec", "ask", " Keep 'ask' unless you fully trust the current model.", group="tools-sys"),
    SettingSpec("exec.timeout_s", "Exec timeout (seconds)", "int", 60,
                "Max wall-clock time per sandboxed command before it is killed.",
                min=1, max=900, group="tools-sys"),
    _tool("datetime",
          "Let the assistant read the server clock (prevents guessing 'today').", group="tools-sys"),
    # --- device ---
    _tool("device_list",
          "List files in an allow-listed folder on a paired device helper. Requires a helper.",
          default=False, group="tools-device"),
    _tool("device_screenshot",
          "Take a screenshot via a paired device helper. Always asks before capturing.",
          default=False, group="tools-device"),
    SettingSpec("device.helper_url", "Device helper URL", "str", "",
                "Base URL of the device helper daemon on one of your devices, e.g. "
                "http://100.101.102.103:8765 (tailnet IP). Leave blank until a helper exists — the "
                "device tools then report that nothing is paired. Overridable per deployment with "
                "HEARTH_DEVICE_HELPER_URL.",
                group="tools-device"),
    SettingSpec("sandbox.backend", "Sandbox backend", "choice", "auto",
                "How exec commands are isolated. 'auto' picks the best available at runtime.",
                choices=["auto", "bwrap", "docker", "stub"], group="tools-sys"),
]

ALL_SPECS: list[SettingSpec] = MODEL_SPECS + AGENT_SPECS + TOOL_SPECS
SPEC_BY_KEY = {s.key: s for s in ALL_SPECS}


def _coerce(spec: SettingSpec, raw: str):
    try:
        if spec.type == "int":
            return int(float(raw))
        if spec.type == "float":
            return float(raw)
        if spec.type == "bool":
            return str(raw).lower() in ("1", "true", "yes", "on")
        # choice: a stored value that isn't one of the allowed options (e.g. left over
        # from when this key was an int/bool before becoming a dropdown) falls back to
        # the default rather than rendering a blank select / feeding a bad CLI flag.
        if spec.type == "choice" and spec.choices:
            s = str(raw)
            return s if s in spec.choices else spec.default
        return raw
    except Exception:
        return spec.default


def get_all() -> dict:
    with session_scope() as s:
        stored = {row.key: row.value for row in s.query(_model()).all()}
    out = {}
    for spec in ALL_SPECS:
        out[spec.key] = _coerce(spec, stored.get(spec.key, "")) if spec.key in stored else spec.default
    return out


def get(key: str):
    spec = SPEC_BY_KEY.get(key)
    with session_scope() as s:
        row = s.get(_model(), key)
    if row is None or spec is None:
        return spec.default if spec else None
    return _coerce(spec, row.value)


def set_many(values: dict) -> None:
    with session_scope() as s:
        M = _model()
        for k, v in values.items():
            spec = SPEC_BY_KEY.get(k)
            if not spec:
                continue
            val = str(_coerce(spec, str(v)))
            row = s.get(M, k)
            if row is None:
                s.add(M(key=k, value=val))
            else:
                row.value = val


def _model():
    from .models import Setting
    return Setting
