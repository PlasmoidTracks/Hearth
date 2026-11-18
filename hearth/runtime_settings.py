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


# The captioner's default instruction lives here so the setting spec shows it as its real
# default, and hearth/captioner.py imports the same constant rather than a second copy that
# could drift. (Import order: runtime_settings must not import captioner, which imports us.)
DEFAULT_CAPTIONER_PROMPT = (
    "Describe this audio in detail for someone who cannot hear it. Cover: speakers "
    "(how many, likely gender and age range, tone of voice, emotion, accent), the exact "
    "words spoken where they are clear, background sounds, music (style, tempo, "
    "instruments), room acoustics or reverberation, and anything unusual such as "
    "interruptions, laughter, silence, or a change of scene. Write connected prose in "
    "the language the audio is spoken in; do not invent content you cannot hear."
)


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
    SettingSpec("model.reasoning_retention", "Reasoning retention", "choice", "keep_last",
                "What to keep of the model's thinking (the <think> / reasoning_content text) once it is "
                "shown. It is ALWAYS shown live and NEVER sent back to the model — this only decides how "
                "much survives in the transcript you see after a reload. 'keep_all' keeps every block; "
                "'keep_last' (default) keeps only the most recent answer's, hiding older ones that are just "
                "replay noise; 'drop' removes them from history but leaves the current one visible; 'hide' "
                "never shows reasoning at all. Nothing here changes what the model receives.",
                choices=["keep_all", "keep_last", "drop", "hide"], group="generation"),
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
    SettingSpec("agent.prefill_progress", "Show prefill progress from the server log", "bool",
                True,
                "While a prompt is being read, llama-server prints its own progress and speed "
                "to its log; Hearth reads those lines to show 'prefill 43 % · ≈1m 20s left' in "
                "the chat. The OpenAI endpoint sends nothing during prefill, so this is the only "
                "way to tell a long prompt from a hang. Works only for a server Hearth launched "
                "(its log lives at runtime/llama-server.log); with HEARTH_LLAMA_BASE_URL pointing "
                "somewhere else there is no percentage to show and none is claimed.",
                group="agent"),
    SettingSpec("agent.tool_output_guard", "Cap tool output sent to the model", "bool", True,
                "Backstop against one careless tool call filling the context window. When on, a "
                "tool result longer than the limit below is cut to its first N characters and the "
                "model is TOLD how much was dropped — the call still succeeds, the loss is announced "
                "rather than hidden. Turn off only to debug exactly what a tool returned.",
                group="agent"),
    SettingSpec("agent.tool_output_max_chars", "Max characters per tool result", "int", 24000,
                "How much of ONE tool result reaches the model when the cap above is on. Counted in "
                "CHARACTERS (about 4 per token), not tokens, so it needs no tokenizer and is what you "
                "can reason about. Set high enough that ordinary file reads are never touched — a read "
                "that gets clipped says so and tells the model to use start_line/end_line. Floor 200; "
                "junk or smaller values fall back to 24000.",
                min=200, max=1000000, group="agent"),
    SettingSpec("agent.tool_output_exempt_tools", "Tools exempt from the output cap", "str", "",
                "Comma-separated tool names whose results are NEVER capped — e.g. fs_read, exec. Use "
                "for a tool you deliberately want to hand over in full. Blank (the default) caps "
                "everything equally. Names are matched exactly against the tool names shown in "
                "Admin → Tools.",
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
    "sync_preview": "Project sync: preview", "sync_apply": "Project sync: apply to folder",
    "device_list": "Device folder listing", "device_screenshot": "Device screenshot",
    "pdf_info": "PDF info (pages/metadata)", "pdf_read": "PDF reading (pages as images)",
    "video_info": "Video info (runtime/fps)", "view_video": "Video viewing (sampled frames)",
    "audio_info": "Audio info (duration/streams)", "describe_audio": "Audio description (captioner model)",
    "generate_image": "Image generation (stable-diffusion.cpp)",
    "context_usage": "Context-window usage (meta)",
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
    # --- linked project folder (DEC-049) ---
    _tool("sync_preview",
          "Let the assistant report how its copy of a linked project folder differs from the "
          "real one. Read-only; writes nothing.", group="tools-project"),
    _tool("sync_apply",
          "Let the assistant write changes back into a linked project folder on disk. This is "
          "the only tool that touches files outside a workspace, so its approval is fixed to "
          "'ask' and every overwritten file is backed up first. Requires a folder to be linked "
          "under Admin → Project.", group="tools-project"),
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
    _approval("sync_apply", "ask",
              " Always keep 'ask': this is the only tool that writes outside a workspace. "
              "'auto' would let the model change files on disk without you seeing it.",
              group="tools-project"),
    SettingSpec("project.max_import_files", "Project import: max files", "int", 4000,
                "How many files are copied into the workspace when a project folder is imported. "
                "Large trees hit this cap first; the import report says when it truncated. Bigger "
                "imports also make the model's file listing less useful — link a subtree instead of "
                "a whole repository where you can.",
                min=1, max=200_000, group="tools-project"),
    SettingSpec("project.max_import_bytes", "Project import: max total bytes", "int", 268435456,
                "Total size budget for an import (default 256 MiB). Files past the budget are not "
                "copied; the import report says when it truncated.",
                min=1024, max=8_589_934_592, group="tools-project"),
    SettingSpec("project.apply_backend", "Project apply backend", "choice", "auto",
                "How changes are written back to a linked folder. 'auto' detects whether this "
                "system supports direct writes (Linux/macOS/Windows do; Android does not) and "
                "refuses if it does not. Force 'stub' to disable write-back entirely while keeping "
                "import and preview working.",
                choices=["auto", "local", "stub"], group="tools-project"),
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
    # --- media: PDFs and video as images ---
    _tool("pdf_info",
          "Let the assistant ask how many pages a PDF has (plus title/author/page size) without "
          "rendering it. Cheap and text-only.", group="tools-media"),
    _tool("pdf_read",
          "Let the assistant READ a PDF by rendering its pages to images and looking at them, so "
          "scanned pages, figures and layout survive — fs_read alone only returns the embedded text "
          "stream. Needs a vision model with an mmproj projector to actually see the pages; without "
          "one it still reports what it rendered. Rendering needs pymupdf (pip install pymupdf) or "
          "poppler-utils / ghostscript on the server.", group="tools-media"),
    _tool("video_info",
          "Let the assistant read a video's runtime, frame rate and resolution without decoding it. "
          "Needs ffprobe (ffmpeg) or opencv-python on the server.", group="tools-media"),
    _tool("view_video",
          "Let the assistant LOOK at a video by decoding a few equally spaced frames (default 8) or "
          "frames at timestamps it chooses — never the whole clip. Needs ffmpeg (or opencv-python) "
          "on the server, plus a vision model with mmproj to see the frames.", group="tools-media"),
    _tool("context_usage",
          "Let the assistant see how full its own context window is (tokens used / total, percent, "
          "measured vs estimated). Read-only bookkeeping — useful for long conversations so it can "
          "summarise before being truncated.", group="tools-media"),
    _tool("audio_info",
          "Let the assistant read an audio file's duration, codec and sample rate without sending "
          "it anywhere. Needs ffprobe (ffmpeg); without it the tool says so instead of guessing.",
          group="tools-audio"),
    _tool("describe_audio",
          "Let the assistant ask the captioner model to describe an audio file (or the audio track "
          "of a video) on demand — voices, emotion, music, ambience. Needs the captioner configured "
          "and enabled under Audio; without it the tool explains what is missing rather than "
          "inventing a description.", group="tools-audio"),
    SettingSpec("media.max_image_edge", "Media: max image edge sent to the model (px)", "int", 1280,
                "Rendered PDF pages and video frames are downscaled so their longest edge is at most "
                "this many pixels before being sent to the model. Smaller = far fewer context tokens "
                "per page; larger keeps small print readable. ~1280 matches what most vision "
                "encoders downsample to anyway. Colour is always preserved (no greyscale). Requires "
                "Pillow (pip install Pillow) — without it, images are sent at render resolution.",
                min=320, max=4096, group="tools-media"),
    _tool("generate_image",
          "Let the assistant DRAW pictures by running stable-diffusion.cpp (`sd-cli`) on this "
          "machine. Off by default, and approval 'ask' by default: each call occupies the machine "
          "for tens of seconds to minutes, so it stays the user's decision to approve. Needs a "
          "binary and a model set under Images; without them the tool explains what is missing "
          "instead of inventing an image. Generated files land in `generated/` in the user's "
          "workspace and are shown back to the model, so it can judge its own result.",
          default=False, group="tools-image"),
    _approval("generate_image", "ask",
              " Keep 'ask': one generation can cost minutes of CPU/GPU. Set 'auto' only if you "
              "trust the current model not to draw repeatedly.", group="tools-image"),

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

# ---- Audio captioner: a SECOND model that listens (DEC-056) ------------------
# Independent of the chat model on purpose: its own llama-server process, port and log, so
# starting or restarting it never disturbs a conversation in progress. Off by default —
# until an admin supplies paths, nothing about it is claimed to work.
CAPTIONER_SPECS: list[SettingSpec] = [
    SettingSpec("captioner.enabled", "Enable the audio captioner", "bool", False,
                "Let Hearth route audio (and the audio track of video) to a SECOND model that "
                "describes what it HEARS — voices, timbre, emotion, music, ambience — and hand "
                "that description to the main model as text. The main model never needs audio "
                "support itself. Leave off until the model paths below are filled in.",
                group="captioner"),
    SettingSpec("captioner.auto_route", "Automatically describe attached audio", "bool", True,
                "When on, an audio attachment is captioned as the message is sent, so the model "
                "already knows what is in it. When off nothing happens automatically — the "
                "assistant can still call `describe_audio` itself when asked. Auto-routing costs "
                "a few seconds (or minutes for a long recording) before the first reply.",
                group="captioner"),
    SettingSpec("captioner.backend", "Captioner backend", "choice", "server",
                "'server' runs a dedicated llama-server that Hearth starts and supervises on its "
                "own port, so the weights stay loaded between captions (fast; needs RAM/VRAM all "
                "the time). 'cli' runs llama-mtmd-cli once per caption instead — slower because it "
                "reloads the model every time, but it frees memory when idle and needs no port.",
                choices=["server", "cli"], group="captioner", requires_reload=True),

    SettingSpec("captioner.model_path", "Captioner model (.gguf) path", "str", "",
                "Absolute path to an AUDIO-capable model, e.g. "
                "/home/you/models/qwen3-omni-30b-a3b-instruct-Q4_K_M.gguf. This is a separate "
                "model from the chat model above — typically a MoE Omni model, which is why CPU "
                "offload is the default below. Leave blank to use HEARTH_CAPTIONER_MODEL_PATH.",
                group="captioner", requires_reload=True),
    SettingSpec("captioner.mmproj_path", "Captioner projector (mmproj) path", "str", "",
                "Absolute path to the multimodal projector that pairs with the captioner model, "
                "e.g. mmproj-qwen3-omni-...-f16.gguf from the SAME repo. Audio goes through it — "
                "without a matching projector the server accepts the file but hears nothing, and "
                "the CLI backend refuses outright.",
                group="captioner", requires_reload=True),
    SettingSpec("captioner.server_bin", "Captioner llama-server binary", "str", "",
                "Absolute path to the llama-server used for captioning. Leave blank to reuse the "
                "main 'llama-server binary path' — usually what you want unless this build differs.",
                group="captioner", requires_reload=True),
    SettingSpec("captioner.cli_bin", "Captioner llama-mtmd-cli path", "str", "",
                "Absolute path to llama-mtmd-cli, used by the 'cli' backend and as the fallback "
                "when the captioner server cannot answer. Leave blank to auto-detect: Hearth looks "
                "for llama-mtmd-cli next to the llama-server binary above, then on PATH.",
                group="captioner"),
    SettingSpec("captioner.port", "Captioner port", "int", 8091,
                "Loopback port for the captioner's own llama-server. Keep it DIFFERENT from the "
                f"chat model's port ({8081}) — two servers cannot share one, and a clash is the "
                "most common reason a captioner will not start.",
                min=1024, max=65535, group="captioner", requires_reload=True),

    # ---- Fit / offload. MoE models are the expected case: many parameters, few active,
    # so they want lots of RAM and gain little from partial VRAM residency.
    SettingSpec("captioner.n_gpu_layers", "Captioner GPU layers (-ngl)", "choice", "0",
                "How many layers to put on the GPU. Default 0 = CPU only, which is the safe "
                "starting point for a MoE Omni model: it keeps RAM free for the chat model and "
                "avoids a half-on-GPU load that thrashes. 'all' (-1) offloads every layer that "
                "fits — fastest when the whole model genuinely fits in VRAM. 'auto' omits the "
                "flag so llama.cpp decides. If captions are slow but RAM is tight, try 8/16/24 to "
                "park just a few layers on the GPU.",
                choices=["0", "auto", "all"], group="captioner", requires_reload=True),
    SettingSpec("captioner.device", "Captioner device (--device)", "str", "",
                "Comma-separated device names for the captioner only (see llama-server "
                "--list-devices). Blank = auto. Use 'none' to force CPU, or name a second GPU so "
                "the chat model and the captioner never compete for the same card.",
                group="captioner", requires_reload=True),
    SettingSpec("captioner.load_mode", "Captioner load mode (-lm)", "choice", "auto",
                "'auto' memory-maps unless a device objects — good for MoE, since mmap lets the OS "
                "page expert weights in on demand. 'mlock' pins the whole model in RAM (faster, but "
                "it must all fit); 'none' reads it plainly.",
                choices=["auto", "mmap", "mlock", "none"], group="captioner", requires_reload=True),
    SettingSpec("captioner.n_ctx", "Captioner context length", "int", 8192,
                "Context window for the captioner server. It only ever sees one audio chunk plus a "
                "short instruction, so a few thousand tokens is plenty and keeps RAM low — this is "
                "NOT the chat model's context. Raise it if you also raise 'Chunk length' a lot.",
                min=512, max=262144, group="captioner", requires_reload=True),
    SettingSpec("captioner.threads", "Captioner CPU threads (-t)", "int", -1,
                "Threads for captioner inference. -1 = auto. Lower it if CPU captions make the "
                "rest of the machine sluggish; a MoE model on CPU is usually thread-bound.",
                min=-1, max=256, group="captioner", requires_reload=True),
    SettingSpec("captioner.flash_attn", "Captioner flash attention", "choice", "auto",
                "'auto' lets llama.cpp choose; 'on'/'off' force it. Mostly relevant only when some "
                "layers are on the GPU.",
                choices=["auto", "on", "off"], group="captioner", requires_reload=True),
    SettingSpec("captioner.extra_args", "Captioner extra args", "str", "",
                "Raw CLI arguments appended verbatim to the captioner launch, e.g. "
                "\"--override-tensor ff.*=CPU\". Use this for anything not covered above; a "
                "malformed value is refused rather than guessed at.",
                group="captioner", requires_reload=True),
    SettingSpec("captioner.startup_timeout_s", "Captioner startup timeout (s)", "int", 300,
                "How long to wait for the captioner server to load its weights before giving up "
                "and reporting why. A 30 B MoE from cold disk can take a couple of minutes; do not "
                "set this below what your machine actually needs or Start looks broken.",
                min=15, max=3600, group="captioner"),

    # ---- What gets sent ----------------------------------------------------------------------------------
    SettingSpec("captioner.chunk_seconds", "Chunk length (seconds)", "int", 30,
                "Long recordings are cut into pieces this long and captioned one at a time, each "
                "labelled with its start time so the main model can say 'at 1:30 she laughs'. Also "
                "bounds how much audio one request carries. Needs ffmpeg; without it the file is "
                "sent whole. 0 = never split.",
                min=0, max=900, group="captioner"),
    SettingSpec("captioner.resample", "Convert to 16 kHz mono first", "bool", True,
                "Pre-convert to 16 kHz mono PCM WAV with ffmpeg before sending — what audio "
                "encoders expect, and it shrinks big stereo/m4a files a lot. Turn off only if your "
                "build prefers the original container. Without ffmpeg this is skipped automatically.",
                group="captioner"),
    SettingSpec("captioner.max_bytes", "Maximum audio size (bytes)", "int", 67108864,
                "Larger attachments are refused with an explanation rather than captioned — a "
                "two-hour recording would stall the turn for many minutes and produce a description "
                "too long to be useful. Default 64 MiB.",
                min=102400, max=8589934592, group="captioner"),
    SettingSpec("captioner.request_timeout_s", "Captioner request timeout (s)", "int", 600,
                "Per-chunk wall-clock limit for both backends. Exceeding it is reported as a "
                "timeout naming the limit, not as a hung chat. Raise together with 'Chunk length' "
                "on slow hardware.",
                min=15, max=3600, group="captioner"),
    SettingSpec("captioner.stream", "Stream the captioner's response", "bool", True,
                "Ask the captioner server to stream its description token by token so the chat can "
                "show it working (in a collapsible block, like the main model's reasoning) instead "
                "of one silent wait. Turn off only for debugging or an unusual build that mishandles "
                "streamed responses — then the caption arrives all at once. The CLI backend always "
                "prints its whole answer at the end regardless.",
                group="captioner"),

    # ---- Generation (applies immediately; no restart needed) ------------------------------
    SettingSpec("captioner.prompt", "Captioning prompt", "str", DEFAULT_CAPTIONER_PROMPT,
                "The instruction sent with every chunk. The default asks for speakers, tone, "
                "emotion, exact words, background sounds, music and acoustics in connected prose. "
                "Make it specific for your use: 'transcribe verbatim with speaker labels', or "
                "'describe only the music: genre, tempo, instruments'. Per-call extras from the "
                "model are appended to this.",
                group="captioner"),
    SettingSpec("captioner.temperature", "Captioner temperature", "float", 0.6,
                "Sampling for the captioner. Lower (0.2-0.4) for transcription-like accuracy; the "
                "0.6 default suits descriptive captions.",
                min=0.0, max=2.0, group="captioner"),
    SettingSpec("captioner.top_p", "Captioner top-p", "float", 0.95,
                "Nucleus cutoff for the captioner.", min=0.0, max=1.0, group="captioner"),
    SettingSpec("captioner.top_k", "Captioner top-k", "int", 20,
                "Top-k sampling for the captioner (sent as top_k to the server, --top-k to the CLI).",
                min=0, max=1000, group="captioner"),
    SettingSpec("captioner.max_tokens", "Captioner max tokens per chunk", "int", 1024,
                "Cap on the description produced for ONE chunk. Long enough for a detailed "
                "paragraph; raise it for dense audio with several speakers.",
                min=32, max=8192, group="captioner"),
    SettingSpec("captioner.seed", "Captioner seed (-1 = random)", "int", -1,
                "Fix this to make captions reproducible while you tune the prompt; leave at -1 for "
                "variety.",
                min=-1, max=2147483647, group="captioner"),
]

# ---- Image generation: stable-diffusion.cpp as an external CLI (DEC-067) -----
# No port and no supervised process, unlike the captioner: `sd-cli` runs per call and exits.
# The cost of that simplicity is a model load per generation, which is why the defaults favour
# a moderate step count and why one call may produce several images. Off until an admin supplies
# paths — nothing about it is claimed to work before then.
DIFFUSION_SPECS: list[SettingSpec] = [
    SettingSpec("diffusion.enabled", "Enable image generation", "bool", False,
                "Let Hearth draw pictures by running stable-diffusion.cpp's `sd-cli` as a "
                "subprocess. The chat model never needs to be multimodal — it asks for an image, "
                "and the finished PNG comes back to it so it can see what it drew. Leave off until "
                "the binary and model paths below are filled in.",
                group="diffusion"),
    SettingSpec("diffusion.cli_bin", "stable-diffusion.cpp CLI path", "str", "",
                "Absolute path to your `sd-cli` executable, e.g. "
                "/home/you/stable-diffusion.cpp/build/bin/sd-cli. Leave blank to use "
                "HEARTH_DIFFUSION_CLI_BIN. Hearth runs it directly (never through a shell) with "
                "the settings below; only the flags documented here are passed, so an unexpected "
                "build fails loudly rather than silently drawing something odd.",
                group="diffusion"),
    SettingSpec("diffusion.model_path", "Diffusion model (.gguf)", "str", "",
                "Absolute path to the main model, e.g. "
                "/home/you/models/sd_xl_base_1.0-Q4_0.gguf or a Flux-dev / Qwen-Image file. For "
                "models whose text encoder is NOT bundled (Flux: `--clip_l` + `--t5xxl`; SD3: "
                "`--clip_l` + `--clip_g`), fill in the encoder paths below too — a missing one is "
                "refused before running rather than producing garbage. Overridable with "
                "HEARTH_DIFFUSION_MODEL_PATH.",
                group="diffusion"),
    SettingSpec("diffusion.clip_l", "Text encoder clip_l (.gguf)", "str", "",
                "Optional. Path to clip_l for Flux / SD3 / other multi-encoder models. Omitted "
                "entirely when blank, so single-file SD1.5/SDXL setups are unaffected.",
                group="diffusion"),
    SettingSpec("diffusion.clip_g", "Text encoder clip_g (.gguf)", "str", "",
                "Optional. Path to clip_g (SD3.x and some SDXL-Ultra models). Omitted when blank.",
                group="diffusion"),
    SettingSpec("diffusion.t5xxl", "Text encoder t5xxl (.gguf)", "str", "",
                "Optional. Path to t5xxl — required for Flux-dev and for Qwen-Image quality. It is "
                "the largest encoder, so a quantised copy (t5xxl_fp8_e4m3) keeps RAM in check.",
                group="diffusion"),
    SettingSpec("diffusion.vae", "Standalone VAE (.gguf)", "str", "",
                "Optional. Path to a separate VAE / image decoder, for models that need one or "
                "when you prefer a cleaner decoder than the bundled one. Omitted when blank.",
                group="diffusion"),
    SettingSpec("diffusion.lora_dir", "LoRA directory", "str", "",
                "Optional directory of LoRA files, passed as `--lora-model-dir`. Hearth does not "
                "let the model choose which LoRA to apply — the library applies what the model file "
                "itself expects, so keep this empty unless you have wired that up deliberately.",
                group="diffusion"),
    SettingSpec("diffusion.backend", "Compute backend (--backend)", "str", "",
                "Blank = stable-diffusion.cpp's own default (usually the first GPU it finds). Use "
                "`cpu` to keep VRAM free for the chat model, or a per-module assignment such as "
                "`clip=cpu,vae=cuda0,diffusion=vulkan0`. Run `sd-cli --list-devices` to see the "
                "device names this build accepts. Overridable with HEARTH_DIFFUSION_BACKEND.",
                group="diffusion"),

    # ---- Defaults for one generation. The model may override most of them, but every value
    # is clamped in hearth/diffusion.py — a chatty model cannot ask for 10 000 steps.
    SettingSpec("diffusion.width", "Default width (px)", "int", 768,
                "Default image width when the assistant does not choose one. Multiples of 64 suit "
                "most models; 512 for SD1.5, 1024 for SDXL/Flux. Clamped to 64-2048 per call.",
                min=64, max=2048, group="diffusion"),
    SettingSpec("diffusion.height", "Default height (px)", "int", 768,
                "Default image height when the assistant does not choose one. Same range and "
                "guidance as width.",
                min=64, max=2048, group="diffusion"),
    SettingSpec("diffusion.steps", "Default steps", "int", 20,
                "Sampling steps per image. The main cost knob: 20 is a good default for most "
                "models (Flux-distilled wants ~4, SDXL 20-30). Clamped to 1-60 per call so one "
                "request cannot tie up the machine.",
                min=1, max=60, group="diffusion"),
    SettingSpec("diffusion.cfg_scale", "Default CFG scale", "float", 4.5,
                "How strictly to follow the prompt. ~1.5 is loose and creative, ~4.5 suits Flux "
                "(which is guidance-distilled), ~7 SDXL/SD1.5. Clamped to 0-30 per call.",
                min=0.0, max=30.0, group="diffusion"),
    SettingSpec("diffusion.strength", "Default denoising strength", "float", 0.75,
                "Used only when generating FROM an image (`init_image`): 0.2 barely changes the "
                "input, 0.75 transforms it while keeping composition.",
                min=0.0, max=1.0, group="diffusion"),
    SettingSpec("diffusion.seed", "Default seed (-1 = random)", "int", -1,
                "Fix this to make every generation reproducible while you tune the model; leave at "
                "-1 for variety. The assistant can still pass its own seed to refine one result.",
                min=-1, max=2147483647, group="diffusion"),
    SettingSpec("diffusion.preview", "Use a fast VAE preview while sampling", "bool", True,
                "Passes `--preview vae` so step previews are decoded cheaply. Hearth always pins "
                "this flag one way or the other: sd-cli defaults to `proj` AND writes ./preview.png "
                "into the working directory, which would litter wherever Hearth happens to run.",
                group="diffusion"),

    # ---- Limits ---------------------------------------------------------------------
    SettingSpec("diffusion.timeout_s", "Generation timeout (s)", "int", 600,
                "Wall-clock limit for ONE sd-cli run, including model load — which is the bulk of "
                "it for a big DiT model on CPU. Exceeding it kills the process and says so, rather "
                "than leaving a chat waiting forever. Raise it for slow hardware or huge models; "
                "lower it to catch a misconfigured build sooner.",
                min=30, max=7200, group="diffusion"),
    SettingSpec("diffusion.busy_timeout_s", "Queue wait (s)", "int", 900,
                "Generations are serialised: two at once share memory bandwidth, make each other "
                "slower, and can OOM the chat model on the same box. This is how long a waiting "
                "request holds before explaining that another image is still being drawn. 0 = fail "
                "immediately when busy.",
                min=0, max=3600, group="diffusion"),
    SettingSpec("diffusion.extra_args", "Diffusion extra args", "str", "",
                "Raw CLI arguments appended verbatim to the sd-cli command, e.g. "
                "\"--offload-to-cpu --flash-attn on\" or \"--vae-tiling\". Use this for anything "
                "not covered above; a malformed value is refused rather than guessed at.",
                group="diffusion"),
]

ALL_SPECS: list[SettingSpec] = MODEL_SPECS + AGENT_SPECS + TOOL_SPECS + CAPTIONER_SPECS + DIFFUSION_SPECS
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
