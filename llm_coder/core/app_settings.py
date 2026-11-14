"""
Persistent application settings via QSettings: server config, agent policy,
tool toggles, web access policy, last project, window geometry.
"""
from PySide6.QtCore import QSettings
from .llama_runtime import ServerConfig
from .caption_fallback import DEFAULT_MODEL_KEY as DEFAULT_CAPTION_MODEL_KEY

ORG = "LLMCoder"
APP = "LLMCoderAgent"


def _as_bool(value, default: bool = False) -> bool:
    """Tolerant bool coercion. `default` is returned for an unrecognized string
    (an absent/None value must not silently become True, and a corrupt value
    must not silently override a default the caller cares about)."""
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        return default
    return bool(value)


class AppSettings:
    def __init__(self):
        self.qs = QSettings(ORG, APP)

    def save_server_config(self, config: ServerConfig):
        self.qs.setValue("server_config", config.to_dict())
        self.qs.sync()

    def load_server_config(self) -> ServerConfig:
        d = self.qs.value("server_config", {})
        if not d:
            return ServerConfig()
        try:
            return ServerConfig.from_dict(dict(d))
        except Exception:
            return ServerConfig()

    def save_agent_policy(self, policy: dict):
        self.qs.setValue("agent_policy", policy)
        self.qs.sync()

    def load_agent_policy(self) -> dict:
        d = self.qs.value("agent_policy", {})
        return dict(d) if d else {}

    def save_tool_toggles(self, toggles: dict):
        self.qs.setValue("tool_toggles", toggles)
        self.qs.sync()

    def load_tool_toggles(self) -> dict:
        d = self.qs.value("tool_toggles", {})
        return dict(d) if d else {}

    def save_subagent_tool_toggles(self, toggles: dict):
        """Per-tool enablement for deployed subagents — kept under its own key so
        it never clobbers (or is clobbered by) the main `tool_toggles`."""
        self.qs.setValue("subagent_tool_toggles", toggles)
        self.qs.sync()

    def load_subagent_tool_toggles(self) -> dict:
        """Empty when unset: the caller (ToolExecutor / Tools tab) fills in the
        default, which is every tool except the hard SUBAGENT_TOOL_BLOCKLIST.
        Storing the delta rather than a full default map means a tool added in a
        later version correctly defaults ON for subagents instead of inheriting
        a stale False baked into an old settings file."""
        d = self.qs.value("subagent_tool_toggles", {})
        return dict(d) if d else {}

    def save_web_policy(self, policy: dict):
        self.qs.setValue("web_policy", policy)
        self.qs.sync()

    def load_web_policy(self) -> dict:
        d = self.qs.value("web_policy", {})
        policy = dict(d) if d else {}
        # Backwards-compatible defaults for keys added after the first version
        # of this setting existed (tor mode support).
        policy.setdefault("mode", "off")
        policy.setdefault("allowlist", [])
        policy.setdefault("blocklist", [])
        policy.setdefault("tor_socks_host", "127.0.0.1")
        policy.setdefault("tor_socks_port", 9050)
        return policy

    def save_script_policy(self, policy: dict):
        self.qs.setValue("script_policy", policy)
        self.qs.sync()

    def load_script_policy(self) -> dict:
        d = self.qs.value("script_policy", {})
        return dict(d) if d else {"mode": "notify", "timeout": 10}  # "notify" | "always_allow"

    def save_vision_policy(self, policy: dict):
        self.qs.setValue("vision_policy", policy)
        self.qs.sync()

    def load_vision_policy(self) -> dict:
        d = self.qs.value("vision_policy", {})
        policy = dict(d) if d else {}
        defaults = {
            "mode": "auto",          # "auto" | "mmproj_only" | "disabled"
            "mmproj_path": "",       # manually overridden path, empty = auto-detect
            # Image-to-text fallback used only when no mmproj projector is loaded.
            "caption_fallback_enabled": False,
            "caption_model": DEFAULT_CAPTION_MODEL_KEY,
            "caption_device": "auto",   # "auto" | "cpu" | "cuda"
        }
        for k, v in defaults.items():
            policy.setdefault(k, v)
        # QSettings round-trips booleans as strings in some backends.
        policy["caption_fallback_enabled"] = _as_bool(policy["caption_fallback_enabled"])
        return policy

    def save_thinking_policy(self, policy: dict):
        self.qs.setValue("thinking_policy", policy)
        self.qs.sync()

    def load_thinking_policy(self) -> dict:
        d = self.qs.value("thinking_policy", {})
        return dict(d) if d else {
            "enable_thinking": True,
            "preserve_thinking": False,   # replay reasoning into context for past turns
            "show_live_thinking": True,   # stream <think> content into the UI
        }

    def save_last_project(self, path: str):
        self.qs.setValue("last_project", path)
        self.qs.sync()

    def load_last_project(self) -> str:
        return self.qs.value("last_project", "")

    def save_geometry(self, geometry):
        self.qs.setValue("window_geometry", geometry)
        self.qs.sync()

    def load_geometry(self):
        return self.qs.value("window_geometry", None)

    # ---- Draft store settings ----

    def save_draft_policy(self, policy: dict):
        self.qs.setValue("draft_policy", policy)
        self.qs.sync()

    def load_draft_policy(self) -> dict:
        d = self.qs.value("draft_policy", {})
        return dict(d) if d else {
            "merge_gap": 15,         # hunks closer than this many lines get merged
            "bulk_threshold": 0.5,   # fraction above which the whole file is one patch
        }

    # ---- Command tool policy ----

    # ---- Web fetch truncation settings ----

    def save_web_fetch_policy(self, policy: dict):
        self.qs.setValue("web_fetch_policy", policy)
        self.qs.sync()

    def load_web_fetch_policy(self) -> dict:
        d = self.qs.value("web_fetch_policy", {})
        if d:
            policy = dict(d)
        else:
            policy = {
                "max_text_chars": 8000,          # max extracted text length
                "max_raw_bytes": 500000,         # max raw HTML download size
                "max_links": 30,                 # max links to include
                "truncation_mode": "show_popup", # "show_popup" | "deny" | "silent"
            }
        # Apply defaults for any missing keys (migration from older settings)
        policy.setdefault("max_text_chars", 8000)
        policy.setdefault("max_raw_bytes", 500000)
        policy.setdefault("max_links", 30)
        policy.setdefault("truncation_mode", "show_popup")
        return policy

    def save_command_policy(self, policy: dict):
        self.qs.setValue("command_policy", policy)
        self.qs.sync()

    def load_command_policy(self) -> dict:
        d = self.qs.value("command_policy", {})
        return dict(d) if d else {
            "mode": "notify",        # "notify" | "always_allow"
            "timeout": 30,
            "allowlist": [],         # list of {"command": ..., "description": ...}
        }

    # ---- Time-based auto-injection settings ----

    def save_time_barriers(self, enabled: bool, barriers: list):
        """Save time barrier config. barriers: list of {"seconds": int, "message": str}"""
        self.qs.setValue("time_barriers", {
            "enabled": enabled,
            "barriers": barriers,
        })
        self.qs.sync()

    def load_time_barriers(self) -> dict:
        d = self.qs.value("time_barriers", {})
        if d:
            policy = dict(d)
        else:
            policy = {"enabled": False, "barriers": []}
        policy.setdefault("enabled", False)
        policy.setdefault("barriers", [])
        return policy

    # ---- File-read pruning settings ----

    def save_prune_aggression(self, aggression: str):
        """aggression: 'off' | 'end_of_turn' | 'after_each_read'"""
        self.qs.setValue("prune_aggression", aggression)
        self.qs.sync()

    def load_prune_aggression(self) -> str:
        val = self.qs.value("prune_aggression", None)
        if val in ("off", "end_of_turn", "after_each_read"):
            return val
        # Backwards-compatible: if old prune_file_reads key exists, map it.
        # The default above must be None, not "off": with "off" as the default
        # the membership test passed on an absent key and returned immediately,
        # so this migration could only ever run for a *corrupt* value and never
        # for an old install that simply had no prune_aggression key yet.
        old = self.qs.value("prune_file_reads", False)
        if _as_bool(old):
            return "end_of_turn"
        return "off"

    def save_delete_tool_keep_count(self, keep_count: int):
        """Save the number of recent tool results to keep per key during deletion compaction."""
        self.qs.setValue("delete_tool_keep_count", max(0, keep_count))
        self.qs.sync()

    def load_delete_tool_keep_count(self) -> int:
        val = self.qs.value("delete_tool_keep_count", 1)
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 1

    def config_dir(self) -> str:
        import os
        ini_path = self.qs.fileName()
        base_dir = os.path.dirname(ini_path)
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def save_subagent_policy(self, policy: dict):
        self.qs.setValue("subagent_policy", policy)
        self.qs.sync()

    def load_subagent_policy(self) -> dict:
        """Subagent model-server settings (QSettings key "subagent_policy").
        Defaults mirror SubagentPolicy exactly (core/subagent_server.py): the
        feature is OFF and unset out of the box, so nothing ever spawns a second
        llama-server for users who don't turn it on.

        `device` is deliberately all-or-nothing — "gpu" (whole model offloaded)
        or "ram" (no offload at all). There is no partial/mixed option on
        purpose: the subagent model is small, and the main model's placement is
        never inherited."""
        d = self.qs.value("subagent_policy", {})
        policy = dict(d) if d else {}
        policy.setdefault("enabled", False)
        policy.setdefault("model_path", "")
        policy.setdefault("binary_path", "")   # "" -> reuse the main binary
        policy.setdefault("host", "127.0.0.1")
        policy.setdefault("port", 8082)        # != main server's 8080
        policy.setdefault("context_size", 4096)
        policy.setdefault("device", "gpu")     # "gpu" | "ram"
        policy.setdefault("startup_timeout", 120)
        policy.setdefault("extra_args", "--jinja")
        # Thinking is a per-subagent switch (NOT inherited from the main agent):
        # worker checkpoints are frequently non-reasoning models, and the caller
        # in core/agent.py reads exactly these two keys.
        policy.setdefault("enable_thinking", True)
        policy.setdefault("preserve_thinking", False)
        # "" = inherit the main server's KV cache type; any KV_CACHE_TYPES
        # member overrides it for the subagent server alone. See
        # SubagentPolicy for why this can only ever be per-process.
        policy.setdefault("kv_cache_type_k", "")
        policy.setdefault("kv_cache_type_v", "")
        if policy["device"] not in ("gpu", "ram"):
            policy["device"] = "gpu"
        # QSettings round-trips booleans as strings in some backends.
        policy["enabled"] = _as_bool(policy["enabled"])
        policy["enable_thinking"] = _as_bool(policy["enable_thinking"], True)
        policy["preserve_thinking"] = _as_bool(policy["preserve_thinking"], False)
        return policy
