"""
Settings dialog: backend + performance + sampling + LoRA/security (all
llama.cpp server flags), agent behavior, web access, tools/safety, and
drafts/time-barriers.

Design notes for future maintainers:
  * get_policy()/get_draft_policy()/get_web_policy()/get_vision_policy()/
    get_command_policy()/get_time_barriers_policy() all MERGE onto a copy of
    the ORIGINAL dict passed into the dialog rather than building a fresh
    dict from scratch. main_window.py replaces its policy dict wholesale
    with whatever these methods return, so any key this dialog doesn't
    expose a control for (e.g. "verbosity", which is owned live by the
    combo box in the main chat panel) MUST survive a Settings save
    unchanged. Building fresh dicts here was the original cause of the
    duplicate-verbosity-control bug — don't reintroduce it.
  * The Vision tab has been removed. The mmproj path lives solely on the
    Backend tab. get_vision_policy() is kept as a thin compatibility shim
    so main_window.py doesn't need to change immediately; it always
    reports vision as disabled/no-fallback.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QFileDialog, QHBoxLayout, QVBoxLayout, QLabel, QComboBox, QTabWidget,
    QWidget, QPlainTextEdit, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QScrollArea
)
from PySide6.QtGui import QColor
import shutil
from core.subagent_server import SubagentPolicy
from core.llama_runtime import (
    ServerConfig, KV_CACHE_TYPES, FLASH_ATTN_MODES, NUMA_MODES,
    LOAD_MODES, SPLIT_MODES, ROPE_SCALING_MODES, REASONING_FORMAT_OPTIONS,
    REASONING_MODES, SPEC_TYPE_OPTIONS, SPEC_TYPES_NO_DRAFT_MODEL,
    list_devices,
)
from core.agent import DEFAULT_SYSTEM_PROMPT, PATCH_MODES
from core.app_settings import AppSettings as _AppSettings
from pathlib import Path
from ui.command_whitelist_tab import CommandWhitelistTab


class SettingsDialog(QDialog):
    def __init__(self, config: ServerConfig, policy: dict, web_policy: dict, vision_policy: dict = None,
                 draft_policy: dict = None, time_barriers_policy: dict = None,
                 subagent_policy: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.config = config
        self.setMinimumWidth(680)
        self.setMinimumHeight(680)

        # The subagent policy must exist BEFORE any tab is built: both
        # _build_backend_tab (model path row) and _build_subagent_group read it.
        # Built from SubagentPolicy so any key the dialog does not expose
        # survives a save.
        self.subagent_policy = SubagentPolicy.from_dict(subagent_policy or {})

        # Keep the ORIGINAL dicts so get_*() can merge instead of clobber.
        self._original_policy = dict(policy or {})
        self._original_vision_policy = dict(vision_policy or {})
        self.draft_policy = draft_policy or {}
        self._original_draft_policy = dict(self.draft_policy)
        self.time_barriers_policy = time_barriers_policy or {"enabled": False, "barriers": []}
        self._original_time_barriers_policy = dict(self.time_barriers_policy)

        self.app_settings = _AppSettings()
        self.web_fetch_policy_dict = self.app_settings.load_web_fetch_policy()
        self._original_web_fetch_policy = dict(self.web_fetch_policy_dict)
        self._original_web_policy = dict(web_policy or {})

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._scrollable(self._build_backend_tab(config)), "Backend")
        tabs.addTab(self._scrollable(self._build_performance_tab(config)), "Performance")
        tabs.addTab(self._scrollable(self._build_sampling_tab(config)), "Sampling")
        tabs.addTab(self._scrollable(self._build_lora_security_tab(config)), "LoRA && Security")
        tabs.addTab(self._scrollable(self._build_behavior_tab(policy)), "Agent Behavior")
        tabs.addTab(self._scrollable(self._build_web_tab(web_policy)), "Web Access")

        self.command_whitelist = CommandWhitelistTab()
        tabs.addTab(self._scrollable(self._build_tools_safety_tab(policy)), "Tools && Safety")
        tabs.addTab(self._scrollable(self._build_drafts_time_tab()), "Drafts && Time Barriers")

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        save_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self._toggle_mode(config.managed)

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        area.setFrameShape(QScrollArea.NoFrame)
        return area

    # ---------- Shared file-picker helper with persisted last-used dir ----------

    def _last_dir(self, category: str, fallback_field_text: str = "") -> str:
        """Resolve a start directory: field's own dir -> last-used dir for this
        category -> last-used dir globally -> empty (OS default)."""
        if fallback_field_text and fallback_field_text.strip():
            p = Path(fallback_field_text.strip())
            if p.exists():
                return str(p.parent if p.is_file() else p)
        get_fn = getattr(self.app_settings, "load_last_browse_dir", None)
        if get_fn:
            d = get_fn(category) or get_fn("_global") or ""
            if d and Path(d).is_dir():
                return d
        return ""

    def _remember_dir(self, category: str, path: str):
        set_fn = getattr(self.app_settings, "save_last_browse_dir", None)
        if set_fn and path:
            d = str(Path(path).resolve().parent)
            set_fn(category, d)
            set_fn("_global", d)

    def _pick_file(self, line_edit: QLineEdit, category: str, title: str, name_filter: str = ""):
        start_dir = self._last_dir(category, line_edit.text())
        path, _ = QFileDialog.getOpenFileName(self, title, start_dir, filter=name_filter)
        if path:
            line_edit.setText(path)
            self._remember_dir(category, path)

    # ---------- Backend ----------

    def _build_backend_tab(self, config):
        tab = QWidget()
        b_layout = QVBoxLayout(tab)

        self.managed_checkbox = QCheckBox("Let app manage llama-server process")
        self.managed_checkbox.setChecked(config.managed)
        self.managed_checkbox.toggled.connect(self._toggle_mode)
        b_layout.addWidget(self.managed_checkbox)

        form = QFormLayout()

        self.binary_edit = QLineEdit(config.binary_path)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(lambda: self._pick_file(self.binary_edit, "binary", "Select llama-server binary"))
        binary_row = QHBoxLayout()
        binary_row.addWidget(self.binary_edit)
        binary_row.addWidget(browse_btn)
        form.addRow("llama-server binary:", binary_row)

        self.model_edit = QLineEdit(config.model_path)
        model_browse_btn = QPushButton("Browse...")
        model_browse_btn.clicked.connect(lambda: self._pick_file(self.model_edit, "model", "Select model file", "GGUF files (*.gguf)"))
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_edit)
        model_row.addWidget(model_browse_btn)
        form.addRow("Model (.gguf) path:", model_row)

        self.mmproj_edit = QLineEdit(config.mmproj_path)
        self.mmproj_browse_btn = QPushButton("Browse...")
        self.mmproj_browse_btn.clicked.connect(
            lambda: self._pick_file(self.mmproj_edit, "model", "Select mmproj file", "GGUF files (*.gguf)")
        )
        self.mmproj_browse_btn.setEnabled(bool(config.model_path))
        mmproj_row = QHBoxLayout()
        mmproj_row.addWidget(self.mmproj_edit)
        mmproj_row.addWidget(self.mmproj_browse_btn)
        form.addRow("Vision projector (mmproj.gguf, optional):", mmproj_row)
        self.model_edit.textChanged.connect(lambda t: self.mmproj_browse_btn.setEnabled(bool(t.strip())))

        # ---- Subagent model (another llama-server child, its own port) ----
        # "Which model" choices live on this form; the tuning knobs live in their
        # own group further down. Always editable, even when the feature is off,
        # so a model can be picked before satisfying the enable switch's own
        # precondition.
        subagent_policy = self.subagent_policy
        self.subagent_model_edit = QLineEdit(subagent_policy.model_path)
        self.subagent_model_edit.setPlaceholderText(
            "Path to a small text GGUF for deployed subagents — required before the switch below can take effect")
        self.subagent_model_browse_btn = QPushButton("Browse...")
        self.subagent_model_browse_btn.clicked.connect(
            lambda: self._pick_file(self.subagent_model_edit, "model",
                                    "Select subagent model file", "GGUF files (*.gguf)")
        )
        subagent_model_row = QHBoxLayout()
        subagent_model_row.addWidget(self.subagent_model_edit)
        subagent_model_row.addWidget(self.subagent_model_browse_btn)
        form.addRow("Subagent model (.gguf, optional):", subagent_model_row)
        self.subagent_model_edit.setEnabled(True)
        self.subagent_model_browse_btn.setEnabled(True)

        self.host_edit = QLineEdit(config.host)
        form.addRow("Host:", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(config.port)
        form.addRow("Port:", self.port_spin)

        self.context_spin = QSpinBox()
        self.context_spin.setRange(512, 1048576)
        self.context_spin.setValue(config.context_size)
        form.addRow("Context size:", self.context_spin)

        self.gpu_layers_spin = QSpinBox()
        self.gpu_layers_spin.setRange(-1, 999)
        self.gpu_layers_spin.setValue(config.gpu_layers)
        self.gpu_layers_spin.setToolTip("-1 = not passed (llama.cpp default: 'auto')")
        form.addRow("GPU layers (-ngl):", self.gpu_layers_spin)

        self.kv_k_combo = QComboBox()
        self.kv_k_combo.addItems(KV_CACHE_TYPES)
        self.kv_k_combo.setCurrentText(config.kv_cache_type_k)
        form.addRow("KV cache type (K):", self.kv_k_combo)

        self.kv_v_combo = QComboBox()
        self.kv_v_combo.addItems(KV_CACHE_TYPES)
        self.kv_v_combo.setCurrentText(config.kv_cache_type_v)
        form.addRow("KV cache type (V):", self.kv_v_combo)

        self.flash_attn_combo = QComboBox()
        self.flash_attn_combo.addItems(FLASH_ATTN_MODES)
        self.flash_attn_combo.setCurrentText(config.flash_attn)
        form.addRow("Flash attention:", self.flash_attn_combo)

        # ---- Device / offload placement ----
        device_group = QGroupBox("Device placement")
        device_form = QFormLayout(device_group)

        self.device_edit = QLineEdit(config.device)
        self.device_edit.setPlaceholderText("e.g. CUDA0,CUDA1 (blank = auto)")
        detect_row = QHBoxLayout()
        detect_row.addWidget(self.device_edit)
        detect_btn = QPushButton("Detect...")
        detect_btn.clicked.connect(self._detect_devices)
        detect_row.addWidget(detect_btn)
        device_form.addRow("Devices (-dev):", detect_row)

        self.split_mode_combo = QComboBox()
        self.split_mode_combo.addItems(SPLIT_MODES)
        self.split_mode_combo.setCurrentText(config.split_mode)
        device_form.addRow("Split mode (-sm):", self.split_mode_combo)

        self.tensor_split_edit = QLineEdit(config.tensor_split)
        self.tensor_split_edit.setPlaceholderText("e.g. 3,1 (proportions per GPU)")
        device_form.addRow("Tensor split (-ts):", self.tensor_split_edit)

        self.override_tensor_edit = QLineEdit(config.override_tensor)
        self.override_tensor_edit.setPlaceholderText("tensor_name_pattern=buffer_type,...")
        device_form.addRow("Override tensor (-ot):", self.override_tensor_edit)

        self.cpu_moe_checkbox = QCheckBox("Keep ALL MoE weights on CPU (-cmoe)")
        self.cpu_moe_checkbox.setChecked(config.cpu_moe)
        device_form.addRow(self.cpu_moe_checkbox)

        self.n_cpu_moe_spin = QSpinBox()
        self.n_cpu_moe_spin.setRange(-1, 999)
        self.n_cpu_moe_spin.setValue(config.n_cpu_moe)
        self.n_cpu_moe_spin.setToolTip("-1 = not passed. Keep MoE weights of the first N layers on CPU.")
        device_form.addRow("N layers MoE on CPU (-ncmoe):", self.n_cpu_moe_spin)

        b_layout.addLayout(form)
        b_layout.addWidget(self._build_subagent_group())
        b_layout.addWidget(device_group)

        self.extra_args_edit = QLineEdit(config.extra_args)
        extra_form = QFormLayout()
        extra_form.addRow("Extra args:", self.extra_args_edit)
        self.external_url_edit = QLineEdit(config.external_base_url)
        extra_form.addRow("External base URL:", self.external_url_edit)
        b_layout.addLayout(extra_form)

        copy_cmd_btn = QPushButton("Copy llama-server command to clipboard")
        copy_cmd_btn.clicked.connect(self._copy_command)
        b_layout.addWidget(copy_cmd_btn)
        hint = QLabel(
            "<i>Tip: quantized KV cache (q8_0/q4_0/etc.) reduces memory use for long context "
            "but may slightly affect quality. --jinja is required for tool calling.</i>"
        )
        hint.setWordWrap(True)
        b_layout.addWidget(hint)
        b_layout.addStretch()
        return tab


    def _build_subagent_group(self) -> QGroupBox:
        """Subagent model server options. The model path itself lives on the main
        Backend form (below the vision projector row); everything else — the
        dedicated port, context size, the GPU/RAM placement choice, its own KV
        cache quantization, and its own thinking switches — is here.

        The device choice is a two-way switch, not a layer count: the subagent
        model is either entirely on the GPU or entirely in RAM. There is
        deliberately no -ngl spin box, because a partial offload is exactly what
        this setting is meant to make unreachable. Flash attention is still
        absent: it is inherited, and overriding it belongs in Extra args."""
        sp = self.subagent_policy
        group = QGroupBox("Subagent model server (separate llama-server for deployed subagents)")
        s_form = QFormLayout(group)

        self.subagent_enabled_checkbox = QCheckBox("Enable subagent model (load it alongside the main model)")
        self.subagent_enabled_checkbox.setChecked(sp.enabled)
        self.subagent_enabled_checkbox.toggled.connect(self._toggle_subagent_mode)
        self.subagent_enabled_checkbox.setToolTip(
            "When on, a second llama-server is started on its own port with this "
            "model, at the same time as the main model, and deployed subagents run "
            "on it instead of on the main model. It does NOT inherit the main "
            "model's sampler, context size, speculative decoding or GPU placement. "
            "The model path above stays editable either way, so you can pick a "
            "model before turning this on."
        )
        s_form.addRow(self.subagent_enabled_checkbox)

        self.subagent_port_spin = QSpinBox()
        self.subagent_port_spin.setRange(1, 65535)
        self.subagent_port_spin.setValue(sp.port)
        self.subagent_port_spin.setToolTip(
            "Port for the subagent server. Must differ from the main server's port "
            "above, or the two processes will fight for the same socket. Default "
            "8082, so it does not clash with the main server's 8080."
        )
        s_form.addRow("Subagent server port:", self.subagent_port_spin)

        self.subagent_context_spin = QSpinBox()
        self.subagent_context_spin.setRange(512, 1_048_576)
        self.subagent_context_spin.setValue(sp.context_size)
        self.subagent_context_spin.setToolTip(
            "Context size of the subagent server only. Each deployment starts from a "
            "fresh context, so this bounds one subagent run, not the session."
        )
        s_form.addRow("Subagent context size (-c):", self.subagent_context_spin)

        self.subagent_device_combo = QComboBox()
        # Order matters: "gpu" is index 0 and is the fallback for a corrupt value.
        self.subagent_device_combo.addItem("GPU — load fully onto the GPU (-ngl all)", "gpu")
        self.subagent_device_combo.addItem("RAM — load fully into system RAM, no offload (-ngl 0)", "ram")
        idx = self.subagent_device_combo.findData(sp.device)
        self.subagent_device_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.subagent_device_combo.setToolTip(
            "Where the WHOLE subagent model lives. Either fully on the GPU or fully "
            "in RAM — a mixed/partial offload is not offered, and the main model's "
            "placement is never inherited. Pick RAM if the GPU has no room left over "
            "after the main model: a small CPU-resident worker is cheap and leaves "
            "the VRAM to the main model."
        )
        s_form.addRow("Subagent model placement:", self.subagent_device_combo)

        self.subagent_startup_spin = QSpinBox()
        self.subagent_startup_spin.setRange(10, 3600)
        self.subagent_startup_spin.setValue(sp.startup_timeout)
        self.subagent_startup_spin.setSuffix(" s")
        self.subagent_startup_spin.setToolTip(
            "How long to wait for the subagent model to load. With 'Start Server' it "
            "loads in parallel with the main model; otherwise it starts lazily on the "
            "first deployment and that call pays the cost."
        )
        s_form.addRow("Model load timeout:", self.subagent_startup_spin)

        self.subagent_binary_edit = QLineEdit(sp.binary_path)
        self.subagent_binary_edit.setPlaceholderText("Blank = reuse the main llama-server binary above")
        subagent_bin_btn = QPushButton("Browse...")
        subagent_bin_btn.clicked.connect(
            lambda: self._pick_file(self.subagent_binary_edit, "binary",
                                    "Select llama-server binary for the subagent model")
        )
        subagent_bin_row = QHBoxLayout()
        subagent_bin_row.addWidget(self.subagent_binary_edit)
        subagent_bin_row.addWidget(subagent_bin_btn)
        s_form.addRow("Subagent llama-server binary:", subagent_bin_row)

        self.subagent_extra_args_edit = QLineEdit(sp.extra_args)
        self.subagent_extra_args_edit.setPlaceholderText("--jinja (required for tool calling)")
        s_form.addRow("Subagent extra args:", self.subagent_extra_args_edit)

        # ---- Subagent KV cache quantization ----
        # The one memory knob this second server can have that differs from the
        # main model, precisely because it IS a separate process: --cache-type-k
        # / --cache-type-v are fixed when the model is loaded, so the OpenAI
        # endpoint offers no per-request equivalent and inheritance used to be
        # the only behaviour available. Empty entry = inherit (the previous
        # behaviour, and the default).
        _kv_items = [("", "Inherit from main server")] + [
            (t, f"{t} (subagent only)") for t in KV_CACHE_TYPES
        ]
        self.subagent_kv_k_combo = QComboBox()
        for _val, _label in _kv_items:
            self.subagent_kv_k_combo.addItem(_label, _val)
        _idx = self.subagent_kv_k_combo.findData(sp.kv_cache_type_k or "")
        self.subagent_kv_k_combo.setCurrentIndex(_idx if _idx >= 0 else 0)
        self.subagent_kv_k_combo.setToolTip(
            "KV cache type for the K cache of the SUBAGENT server only (--cache-type-k).\n\n"
            "This is a startup flag baked into the second llama-server's command line, so it "
            "can differ from the main model — but only because the subagent is a separate "
            "process. It cannot be varied per request.\n\n"
            "'Inherit from main server' keeps the previous behaviour. A quantized type "
            "(q8_0 / q4_0 / ...) cuts the worker's memory use for long contexts at a small "
            "quality cost, and generally needs flash attention enabled to actually pay off — "
            "flash attention is still inherited from the main server, so override it in Extra "
            "args (e.g. --flash-attn on) if you quantize here.\n\n"
            "Changing this restarts the subagent server; the main model is untouched."
        )
        s_form.addRow("Subagent KV cache type (K):", self.subagent_kv_k_combo)

        self.subagent_kv_v_combo = QComboBox()
        for _val, _label in _kv_items:
            self.subagent_kv_v_combo.addItem(_label, _val)
        _idx = self.subagent_kv_v_combo.findData(sp.kv_cache_type_v or "")
        self.subagent_kv_v_combo.setCurrentIndex(_idx if _idx >= 0 else 0)
        self.subagent_kv_v_combo.setToolTip(
            "KV cache type for the V cache of the SUBAGENT server only (--cache-type-v). "
            "Same rules as the K cache above: startup-only, inherited unless you pick a "
            "type here, and restarting the subagent server to apply it."
        )
        s_form.addRow("Subagent KV cache type (V):", self.subagent_kv_v_combo)

        # ---- Subagent thinking ----
        # Deliberately NOT inherited from the main agent's thinking setting: a
        # worker is often a small non-reasoning checkpoint, and the two models
        # have no reason to agree. Read per deployment in
        # core/agent.py::_deploy_subagent.
        self.subagent_enable_thinking_checkbox = QCheckBox(
            "Enable thinking/reasoning for the subagent")
        self.subagent_enable_thinking_checkbox.setChecked(sp.enable_thinking)
        self.subagent_enable_thinking_checkbox.setToolTip(
            "Send chat_template_kwargs {\"enable_thinking\": true} with the subagent's "
            "requests. Independent of the main agent's own thinking setting — the worker "
            "model is usually a different checkpoint and often has no reasoning template "
            "at all.\n\n"
            "Turn this OFF for a worker that should just answer (a non-reasoning or "
            "instruction model): it skips the <think> structure entirely, which is faster "
            "and keeps the worker's one-shot answer clean. Leaving it on for a model that "
            "supports it is fine — the worker's thinking is shown in its own bubble and is "
            "never returned to the deploying agent.\n\n"
            "Takes effect on the next deployment; no server restart needed."
        )
        s_form.addRow(self.subagent_enable_thinking_checkbox)

        self.subagent_preserve_thinking_checkbox = QCheckBox(
            "Replay the subagent's reasoning into its own later turns")
        self.subagent_preserve_thinking_checkbox.setChecked(sp.preserve_thinking)
        self.subagent_preserve_thinking_checkbox.setToolTip(
            "chat_template_kwargs {\"preserve_thinking\": true}: the worker's earlier "
            "reasoning is fed back into its context on its subsequent tool rounds.\n\n"
            "Only meaningful when thinking is enabled above and the template supports it. "
            "It costs context in a run that is bounded by max rounds to begin with, so it is "
            "off by default. It never affects the deploying agent: only the worker's final "
            "message is ever returned."
        )
        s_form.addRow(self.subagent_preserve_thinking_checkbox)

        hint = QLabel(
            "<i>Runs as its own llama-server process on its own port, so it can be "
            "up at the same time as the main model. It inherits this app's "
            "flash-attention mode, thread count and load mode, and nothing else — "
            "the sampler, context size, reasoning flags and speculative decoding of "
            "the main model are not applied to it. The KV cache types above default "
            "to inheriting the main server's and can be overridden per subagent, "
            "because the KV buffer's type is fixed at model-load time and a separate "
            "process is the only way to differ. The placement above is all-or-nothing "
            "by design; put e.g. <b>--flash-attn on</b> in Extra args to override an "
            "inherited value.</i>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a8a8a; font-size:11px;")
        s_form.addRow(hint)

        return group

    def _detect_devices(self):
        binary = self.binary_edit.text().strip() or "llama-server"
        try:
            output = list_devices(binary)
        except Exception as e:
            QMessageBox.warning(self, "Detect devices failed", f"Could not run '{binary} --list-devices':\n{e}")
            return
        QMessageBox.information(self, "Available devices", output or "(no output)")

    # ---------- Performance (llama.cpp) ----------

    def _build_performance_tab(self, config):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        threading_group = QGroupBox("Threading && batching")
        form = QFormLayout(threading_group)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(-1, 256)
        self.threads_spin.setValue(config.threads)
        self.threads_spin.setToolTip("-1 = not passed (llama.cpp default)")
        form.addRow("--threads:", self.threads_spin)

        self.threads_batch_spin = QSpinBox()
        self.threads_batch_spin.setRange(-1, 256)
        self.threads_batch_spin.setValue(config.threads_batch)
        form.addRow("--threads-batch:", self.threads_batch_spin)

        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setRange(-1, 65536)
        self.batch_size_spin.setValue(config.batch_size)
        form.addRow("--batch-size:", self.batch_size_spin)

        self.ubatch_size_spin = QSpinBox()
        self.ubatch_size_spin.setRange(-1, 65536)
        self.ubatch_size_spin.setValue(config.ubatch_size)
        form.addRow("--ubatch-size:", self.ubatch_size_spin)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(-1, 64)
        self.parallel_spin.setValue(config.parallel_slots)
        self.parallel_spin.setToolTip("-1 = auto (llama.cpp default)")
        form.addRow("--parallel (slots, -np):", self.parallel_spin)

        self.cont_batching_checkbox = QCheckBox("--cont-batching (continuous batching)")
        self.cont_batching_checkbox.setChecked(config.cont_batching)
        self.cont_batching_checkbox.setToolTip("Enabled by default on this llama.cpp build.")
        form.addRow(self.cont_batching_checkbox)

        self.numa_combo = QComboBox()
        self.numa_combo.addItems(NUMA_MODES)
        self.numa_combo.setCurrentText(config.numa_mode)
        form.addRow("--numa mode:", self.numa_combo)

        layout.addWidget(threading_group)

        memory_group = QGroupBox("Memory / loading")
        mem_form = QFormLayout(memory_group)

        self.load_mode_combo = QComboBox()
        self.load_mode_combo.addItems(LOAD_MODES)
        self.load_mode_combo.setCurrentText(config.load_mode)
        self.load_mode_combo.setToolTip(
            "-lm: none | mmap | mlock | mmap+mlock | dio.\n"
            "Replaces the deprecated --mlock / --no-mmap flags."
        )
        mem_form.addRow("Load mode (-lm):", self.load_mode_combo)

        self.no_kv_offload_checkbox = QCheckBox("--no-kv-offload (disable KV cache GPU offloading)")
        self.no_kv_offload_checkbox.setChecked(config.no_kv_offload)
        mem_form.addRow(self.no_kv_offload_checkbox)

        layout.addWidget(memory_group)

        context_group = QGroupBox("Context behavior")
        ctx_form = QFormLayout(context_group)

        self.n_predict_spin = QSpinBox()
        self.n_predict_spin.setRange(-1, 1000000)
        self.n_predict_spin.setValue(config.n_predict)
        self.n_predict_spin.setToolTip("-1 = infinity (default). Max tokens to predict per request.")
        ctx_form.addRow("-n / --n-predict:", self.n_predict_spin)

        self.keep_tokens_spin = QSpinBox()
        self.keep_tokens_spin.setRange(-1, 1000000)
        self.keep_tokens_spin.setValue(config.keep_tokens)
        ctx_form.addRow("--keep:", self.keep_tokens_spin)

        self.context_shift_checkbox = QCheckBox("--context-shift (shift context on overflow instead of truncating)")
        self.context_shift_checkbox.setChecked(config.context_shift)
        ctx_form.addRow(self.context_shift_checkbox)

        self.rope_scaling_combo = QComboBox()
        self.rope_scaling_combo.addItems(ROPE_SCALING_MODES)
        self.rope_scaling_combo.setCurrentText(config.rope_scaling)
        ctx_form.addRow("--rope-scaling:", self.rope_scaling_combo)

        self.rope_scale_spin = QDoubleSpinBox()
        self.rope_scale_spin.setRange(0.0, 100.0)
        self.rope_scale_spin.setDecimals(3)
        self.rope_scale_spin.setValue(config.rope_scale)
        self.rope_scale_spin.setToolTip("0 = not passed. Context scaling factor.")
        ctx_form.addRow("--rope-scale:", self.rope_scale_spin)

        self.rope_freq_base_spin = QDoubleSpinBox()
        self.rope_freq_base_spin.setRange(0.0, 10_000_000.0)
        self.rope_freq_base_spin.setDecimals(1)
        self.rope_freq_base_spin.setValue(config.rope_freq_base)
        ctx_form.addRow("--rope-freq-base:", self.rope_freq_base_spin)

        self.rope_freq_scale_spin = QDoubleSpinBox()
        self.rope_freq_scale_spin.setRange(0.0, 100.0)
        self.rope_freq_scale_spin.setDecimals(3)
        self.rope_freq_scale_spin.setValue(config.rope_freq_scale)
        ctx_form.addRow("--rope-freq-scale:", self.rope_freq_scale_spin)

        self.yarn_orig_ctx_spin = QSpinBox()
        self.yarn_orig_ctx_spin.setRange(0, 1048576)
        self.yarn_orig_ctx_spin.setValue(config.yarn_orig_ctx)
        ctx_form.addRow("--yarn-orig-ctx:", self.yarn_orig_ctx_spin)

        layout.addWidget(context_group)

        reasoning_group = QGroupBox("Reasoning")
        rea_form = QFormLayout(reasoning_group)

        self.reasoning_preserve_checkbox = QCheckBox("--reasoning-preserve (retain reasoning across turns, server-side)")
        self.reasoning_preserve_checkbox.setChecked(config.reasoning_preserve)
        rea_form.addRow(self.reasoning_preserve_checkbox)

        self.reasoning_format_combo = QComboBox()
        self.reasoning_format_combo.addItems(REASONING_FORMAT_OPTIONS)
        self.reasoning_format_combo.setCurrentText(config.reasoning_format)
        rea_form.addRow("--reasoning-format:", self.reasoning_format_combo)

        self.reasoning_mode_combo = QComboBox()
        self.reasoning_mode_combo.addItems(REASONING_MODES)
        self.reasoning_mode_combo.setCurrentText(config.reasoning_mode)
        rea_form.addRow("--reasoning (-rea):", self.reasoning_mode_combo)

        self.reasoning_budget_spin = QSpinBox()
        self.reasoning_budget_spin.setRange(-1, 1000000)
        self.reasoning_budget_spin.setValue(config.reasoning_budget)
        self.reasoning_budget_spin.setToolTip("-1 = unrestricted, 0 = immediate end, N>0 = token budget.")
        rea_form.addRow("--reasoning-budget:", self.reasoning_budget_spin)

        self.reasoning_budget_message_edit = QLineEdit(config.reasoning_budget_message)
        self.reasoning_budget_message_edit.setPlaceholderText("Blank = no custom message")
        self.reasoning_budget_message_edit.setToolTip(
            "Message injected before the end-of-thinking tag when the budget is exhausted. "
            "Only takes effect when --reasoning-budget is >= 0. Blank = llama.cpp default (none)."
        )
        rea_form.addRow("--reasoning-budget-message:", self.reasoning_budget_message_edit)

        layout.addWidget(reasoning_group)

        # ---- Speculative decoding ----
        spec_group = QGroupBox("Speculative decoding")
        spec_layout = QVBoxLayout(spec_group)
        spec_form = QFormLayout()

        self.spec_type_list = QTableWidget(len(SPEC_TYPE_OPTIONS), 1)
        self.spec_type_list.setHorizontalHeaderLabels(["Enabled types (comma-joined into --spec-type)"])
        self.spec_type_list.verticalHeader().setVisible(False)
        self.spec_type_list.horizontalHeader().setStretchLastSection(True)
        self.spec_type_list.setMaximumHeight(160)
        current_types = set(t for t in (config.speculative_type or "none").split(",") if t)
        for i, opt in enumerate(SPEC_TYPE_OPTIONS):
            item = QTableWidgetItem(opt)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if opt in current_types else Qt.Unchecked)
            self.spec_type_list.setItem(i, 0, item)
        spec_layout.addWidget(self.spec_type_list)

        self.spec_n_max_spin = QSpinBox()
        self.spec_n_max_spin.setRange(1, 64)
        self.spec_n_max_spin.setValue(config.speculative_draft_n_max)
        spec_form.addRow("--spec-draft-n-max:", self.spec_n_max_spin)

        self.spec_n_min_spin = QSpinBox()
        self.spec_n_min_spin.setRange(0, 64)
        self.spec_n_min_spin.setValue(config.speculative_draft_n_min)
        spec_form.addRow("--spec-draft-n-min:", self.spec_n_min_spin)

        self.spec_model_edit = QLineEdit(config.speculative_draft_model)
        self.spec_model_edit.setPlaceholderText("Path to external draft GGUF model (for -md)")
        spec_model_browse_btn = QPushButton("Browse...")
        spec_model_browse_btn.clicked.connect(
            lambda: self._pick_file(self.spec_model_edit, "model", "Select draft model GGUF", "GGUF files (*.gguf)")
        )
        spec_model_row = QHBoxLayout()
        spec_model_row.addWidget(self.spec_model_edit)
        spec_model_row.addWidget(spec_model_browse_btn)
        spec_form.addRow("Draft model (-md):", spec_model_row)

        self.spec_hf_edit = QLineEdit(config.speculative_draft_hf)
        self.spec_hf_edit.setPlaceholderText("HF repo for draft, e.g. z-lab/Qwen3.6-35B-A3B-DFlash:Q6_K")
        spec_form.addRow("Draft model (HF):", self.spec_hf_edit)

        spec_layout.addLayout(spec_form)
        spec_hint = QLabel(
            "<i>draft-mtp and ngram-* types can work without a draft model, but only when the "
            "MTP head is embedded in the main GGUF. Models that ship the head as a separate "
            "sidecar file (unsloth's Qwen3.8-Flash-Next / Qwen3.6, Gemma 4 assistant heads) "
            "need it set here even for draft-mtp — llama.cpp does not auto-discover sidecars in "
            "subfolders. draft-simple / draft-eagle3 / draft-dflash / draft-dspark always "
            "require either a local -md path or an HF repo above.</i>"
        )
        spec_hint.setWordWrap(True)
        spec_hint.setStyleSheet("color:#8a8a8a; font-size:11px;")
        spec_layout.addWidget(spec_hint)
        layout.addWidget(spec_group)

        note = QLabel(
            "<i>These map directly to llama-server flags on build b06aa774c (version 10287). "
            "Run 'llama-server --help' to confirm flag availability if you update your build.</i>"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return tab

    def _selected_spec_types(self) -> str:
        chosen = []
        for i in range(self.spec_type_list.rowCount()):
            item = self.spec_type_list.item(i, 0)
            if item and item.checkState() == Qt.Checked:
                chosen.append(item.text())
        if not chosen or chosen == ["none"]:
            return "none"
        return ",".join(t for t in chosen if t != "none")

    # ---------- Sampling ----------

    def _build_sampling_tab(self, config):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        intro = QLabel(
            "Server-side sampling defaults, used for any request that doesn't override them "
            "itself. These map to llama-server's '----- sampling params -----' section."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()

        def _dspin(value, lo, hi, step, decimals=2):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(decimals)
            s.setValue(value)
            return s

        self.temperature_spin = _dspin(config.temperature, 0.0, 5.0, 0.05)
        form.addRow("--temp:", self.temperature_spin)

        self.top_k_spin = QSpinBox()
        self.top_k_spin.setRange(0, 1000)
        self.top_k_spin.setValue(config.top_k)
        self.top_k_spin.setToolTip("0 = disabled")
        form.addRow("--top-k:", self.top_k_spin)

        self.top_p_spin = _dspin(config.top_p, 0.0, 1.0, 0.01)
        self.top_p_spin.setToolTip("1.0 = disabled")
        form.addRow("--top-p:", self.top_p_spin)

        self.min_p_spin = _dspin(config.min_p, 0.0, 1.0, 0.01)
        self.min_p_spin.setToolTip("0.0 = disabled")
        form.addRow("--min-p:", self.min_p_spin)

        self.typical_p_spin = _dspin(config.typical_p, 0.0, 1.0, 0.01)
        self.typical_p_spin.setToolTip("1.0 = disabled")
        form.addRow("--typical:", self.typical_p_spin)

        self.repeat_last_n_spin = QSpinBox()
        self.repeat_last_n_spin.setRange(0, 10000)
        self.repeat_last_n_spin.setValue(config.repeat_last_n)
        self.repeat_last_n_spin.setToolTip("0 = disabled")
        form.addRow("--repeat-last-n:", self.repeat_last_n_spin)

        self.repeat_penalty_spin = _dspin(config.repeat_penalty, 0.0, 5.0, 0.01)
        self.repeat_penalty_spin.setToolTip("1.0 = disabled")
        form.addRow("--repeat-penalty:", self.repeat_penalty_spin)

        self.presence_penalty_spin = _dspin(config.presence_penalty, -5.0, 5.0, 0.01)
        form.addRow("--presence-penalty:", self.presence_penalty_spin)

        self.frequency_penalty_spin = _dspin(config.frequency_penalty, -5.0, 5.0, 0.01)
        form.addRow("--frequency-penalty:", self.frequency_penalty_spin)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(-1, 2_147_483_647)
        self.seed_spin.setValue(config.seed)
        self.seed_spin.setToolTip("-1 = random seed")
        form.addRow("--seed:", self.seed_spin)

        layout.addLayout(form)

        dry_group = QGroupBox("DRY repetition penalty")
        dry_form = QFormLayout(dry_group)
        self.dry_multiplier_spin = _dspin(config.dry_multiplier, 0.0, 5.0, 0.05)
        self.dry_multiplier_spin.setToolTip("0.0 = disabled")
        dry_form.addRow("--dry-multiplier:", self.dry_multiplier_spin)
        self.dry_base_spin = _dspin(config.dry_base, 1.0, 5.0, 0.05)
        dry_form.addRow("--dry-base:", self.dry_base_spin)
        self.dry_allowed_length_spin = QSpinBox()
        self.dry_allowed_length_spin.setRange(1, 100)
        self.dry_allowed_length_spin.setValue(config.dry_allowed_length)
        dry_form.addRow("--dry-allowed-length:", self.dry_allowed_length_spin)
        self.dry_penalty_last_n_spin = QSpinBox()
        self.dry_penalty_last_n_spin.setRange(0, 10000)
        self.dry_penalty_last_n_spin.setValue(config.dry_penalty_last_n)
        dry_form.addRow("--dry-penalty-last-n:", self.dry_penalty_last_n_spin)
        layout.addWidget(dry_group)

        mirostat_group = QGroupBox("Mirostat (overrides top-k/top-p/typical when enabled)")
        miro_form = QFormLayout(mirostat_group)
        self.mirostat_combo = QComboBox()
        self.mirostat_combo.addItems(["0 (off)", "1 (Mirostat)", "2 (Mirostat 2.0)"])
        self.mirostat_combo.setCurrentIndex(max(0, min(2, config.mirostat)))
        miro_form.addRow("--mirostat:", self.mirostat_combo)
        self.mirostat_lr_spin = _dspin(config.mirostat_lr, 0.0, 5.0, 0.01)
        miro_form.addRow("--mirostat-lr:", self.mirostat_lr_spin)
        self.mirostat_ent_spin = _dspin(config.mirostat_ent, 0.0, 20.0, 0.1, decimals=2)
        miro_form.addRow("--mirostat-ent:", self.mirostat_ent_spin)
        layout.addWidget(mirostat_group)

        reset_btn = QPushButton("Reset sampling to llama.cpp defaults")
        reset_btn.clicked.connect(self._reset_sampling_defaults)
        layout.addWidget(reset_btn)
        layout.addStretch()
        return tab

    def _reset_sampling_defaults(self):
        d = ServerConfig()
        self.temperature_spin.setValue(d.temperature)
        self.top_k_spin.setValue(d.top_k)
        self.top_p_spin.setValue(d.top_p)
        self.min_p_spin.setValue(d.min_p)
        self.typical_p_spin.setValue(d.typical_p)
        self.repeat_last_n_spin.setValue(d.repeat_last_n)
        self.repeat_penalty_spin.setValue(d.repeat_penalty)
        self.presence_penalty_spin.setValue(d.presence_penalty)
        self.frequency_penalty_spin.setValue(d.frequency_penalty)
        self.dry_multiplier_spin.setValue(d.dry_multiplier)
        self.dry_base_spin.setValue(d.dry_base)
        self.dry_allowed_length_spin.setValue(d.dry_allowed_length)
        self.dry_penalty_last_n_spin.setValue(d.dry_penalty_last_n)
        self.mirostat_combo.setCurrentIndex(0)
        self.mirostat_lr_spin.setValue(d.mirostat_lr)
        self.mirostat_ent_spin.setValue(d.mirostat_ent)
        self.seed_spin.setValue(d.seed)

    # ---------- LoRA & Security ----------

    def _build_lora_security_tab(self, config):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        lora_group = QGroupBox("LoRA adapters (--lora-scaled)")
        lora_layout = QVBoxLayout(lora_group)
        self.lora_table = QTableWidget(0, 2)
        self.lora_table.setHorizontalHeaderLabels(["Adapter path (.gguf)", "Scale"])
        header = self.lora_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.lora_table.verticalHeader().setVisible(False)
        for adapter in (config.lora_adapters or []):
            self._add_lora_row(adapter.get("path", ""), adapter.get("scale", 1.0))
        lora_layout.addWidget(self.lora_table)

        lora_btn_row = QHBoxLayout()
        add_lora_btn = QPushButton("+ Add adapter...")
        add_lora_btn.clicked.connect(self._browse_add_lora)
        remove_lora_btn = QPushButton("- Remove selected")
        remove_lora_btn.clicked.connect(self._remove_lora_row)
        lora_btn_row.addWidget(add_lora_btn)
        lora_btn_row.addWidget(remove_lora_btn)
        lora_btn_row.addStretch()
        lora_layout.addLayout(lora_btn_row)
        layout.addWidget(lora_group)

        sec_group = QGroupBox("Server exposure && security")
        sec_form = QFormLayout(sec_group)

        self.api_key_edit = QLineEdit(config.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        sec_form.addRow("--api-key:", self.api_key_edit)

        self.api_key_file_edit = QLineEdit(config.api_key_file)
        api_key_file_btn = QPushButton("Browse...")
        api_key_file_btn.clicked.connect(
            lambda: self._pick_file(self.api_key_file_edit, "misc", "Select API key file")
        )
        api_key_file_row = QHBoxLayout()
        api_key_file_row.addWidget(self.api_key_file_edit)
        api_key_file_row.addWidget(api_key_file_btn)
        sec_form.addRow("--api-key-file:", api_key_file_row)

        self.cors_origins_edit = QLineEdit(config.cors_origins)
        sec_form.addRow("--cors-origins:", self.cors_origins_edit)

        self.enable_metrics_checkbox = QCheckBox("--metrics (expose Prometheus metrics endpoint)")
        self.enable_metrics_checkbox.setChecked(config.enable_metrics)
        sec_form.addRow(self.enable_metrics_checkbox)

        self.disable_webui_checkbox = QCheckBox("--no-webui (disable llama.cpp's built-in web UI)")
        self.disable_webui_checkbox.setChecked(config.disable_webui)
        sec_form.addRow(self.disable_webui_checkbox)

        self.disable_slots_checkbox = QCheckBox("--no-slots (disable /slots monitoring endpoint)")
        self.disable_slots_checkbox.setChecked(config.disable_slots_endpoint)
        sec_form.addRow(self.disable_slots_checkbox)

        layout.addWidget(sec_group)

        warn = QLabel(
            "<i>Only relevant if the server is reachable from outside localhost. If you only "
            "ever connect from this machine, these can be left at their defaults.</i>"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#8a8a8a;")
        layout.addWidget(warn)
        layout.addStretch()
        return tab

    def _add_lora_row(self, path: str, scale: float):
        row = self.lora_table.rowCount()
        self.lora_table.insertRow(row)
        self.lora_table.setItem(row, 0, QTableWidgetItem(path))
        self.lora_table.setItem(row, 1, QTableWidgetItem(str(scale)))

    def _browse_add_lora(self):
        start_dir = self._last_dir("model")
        path, _ = QFileDialog.getOpenFileName(self, "Select LoRA adapter", start_dir, filter="GGUF files (*.gguf)")
        if path:
            self._remember_dir("model", path)
            self._add_lora_row(path, 1.0)

    def _remove_lora_row(self):
        row = self.lora_table.currentRow()
        if row >= 0:
            self.lora_table.removeRow(row)

    def _collect_lora_adapters(self) -> list:
        result = []
        for row in range(self.lora_table.rowCount()):
            path_item = self.lora_table.item(row, 0)
            scale_item = self.lora_table.item(row, 1)
            path = path_item.text().strip() if path_item else ""
            if not path:
                continue
            try:
                scale = float(scale_item.text()) if scale_item else 1.0
            except ValueError:
                scale = 1.0
            result.append({"path": path, "scale": scale})
        return result

    # ---------- Agent Behavior ----------

    def _build_behavior_tab(self, policy):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel("System prompt:"))
        self.system_prompt_edit = QPlainTextEdit(policy.get("system_prompt", DEFAULT_SYSTEM_PROMPT))
        self.system_prompt_edit.setMinimumHeight(120)
        layout.addWidget(self.system_prompt_edit)
        reset_btn = QPushButton("Reset to default")
        reset_btn.clicked.connect(lambda: self.system_prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT))
        layout.addWidget(reset_btn)

        form = QFormLayout()

        self.patch_mode_combo = QComboBox()
        self.patch_mode_combo.addItems(PATCH_MODES)
        self.patch_mode_combo.setCurrentText(policy.get("patch_mode", "strict_review"))
        form.addRow("Change queue mode:", self.patch_mode_combo)

        self.batch_max_spin = QSpinBox()
        self.batch_max_spin.setRange(1, 50)
        self.batch_max_spin.setValue(policy.get("batch_review_max", 5))
        form.addRow("Batch review max (batch_review mode):", self.batch_max_spin)

        self.max_rounds_spin = QSpinBox()
        self.max_rounds_spin.setRange(1, 1000)
        self.max_rounds_spin.setValue(policy.get("max_tool_rounds", 6))
        form.addRow("Max tool rounds per turn:", self.max_rounds_spin)

        self.overflow_combo = QComboBox()
        self.overflow_combo.addItems(["warn_and_ask", "auto_expand", "auto_compact", "abort"])
        self.overflow_combo.setCurrentText(policy.get("context_overflow_policy", "warn_and_ask"))
        form.addRow("Context overflow policy:", self.overflow_combo)

        self.expand_percent_spin = QSpinBox()
        self.expand_percent_spin.setRange(5, 200)
        self.expand_percent_spin.setValue(policy.get("auto_expand_percent", 20))
        self.expand_percent_spin.setSuffix(" %")
        form.addRow("Auto-expand amount:", self.expand_percent_spin)

        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(["keep_all", "keep_last", "drop", "hide"])
        self.reasoning_combo.setCurrentText(policy.get("reasoning_retention", "keep_last"))
        form.addRow("Reasoning retention:", self.reasoning_combo)

        self.render_combo = QComboBox()
        self.render_combo.addItems(["markdown", "plaintext", "monospace"])
        self.render_combo.setCurrentText(policy.get("chat_render_mode", "markdown"))
        form.addRow("Chat render mode:", self.render_combo)

        self.force_wrap_checkbox = QCheckBox("Word-wrap chat messages")
        self.force_wrap_checkbox.setChecked(policy.get("force_wrap", True))
        self.force_wrap_checkbox.setToolTip(
            "When checked, message text is force-wrapped to the chat panel width. "
            "When unchecked, long lines are not wrapped — the panel grows horizontally "
            "and a horizontal scrollbar appears instead."
        )
        form.addRow(self.force_wrap_checkbox)

        self.auto_scroll_checkbox = QCheckBox("Auto-scroll to bottom on new messages")
        self.auto_scroll_checkbox.setChecked(policy.get("auto_scroll_to_bottom", False))
        self.auto_scroll_checkbox.setToolTip(
            "When checked, the chat follows the conversation: every new message and every "
            "streamed chunk scrolls the view to the very bottom.\n\n"
            "When unchecked, the chat never scrolls on its own — it stays exactly where you "
            "left it.\n\n"
            "Either way, scrolling up away from the bottom interrupts the follow until you "
            "return to the bottom."
        )
        form.addRow(self.auto_scroll_checkbox)

        note_verbosity = QLabel(
            "<i>Verbosity is controlled live from the dropdown above the chat input — "
            "it applies immediately and doesn't need a Settings save.</i>"
        )
        note_verbosity.setWordWrap(True)
        note_verbosity.setStyleSheet("color:#8a8a8a;")
        form.addRow(note_verbosity)

        self.stream_to_chat_checkbox = QCheckBox("Stream response text into chat")
        self.stream_to_chat_checkbox.setChecked(policy.get("stream_to_chat", True))
        self.stream_to_chat_checkbox.setToolTip(
            "Show the model's response in chat as it's generated. Disabled = response appears "
            "all at once after generation finishes."
        )
        form.addRow(self.stream_to_chat_checkbox)

        self.enable_thinking_checkbox = QCheckBox("Enable model thinking/reasoning")
        self.enable_thinking_checkbox.setChecked(policy.get("enable_thinking", True))
        form.addRow(self.enable_thinking_checkbox)

        self.preserve_thinking_checkbox = QCheckBox("Replay reasoning into context on later turns")
        self.preserve_thinking_checkbox.setChecked(policy.get("preserve_thinking", False))
        form.addRow(self.preserve_thinking_checkbox)

        self.prune_aggression_combo = QComboBox()
        self.prune_aggression_combo.addItems(["off", "After each read", "End of turn"])
        key_to_display = {"off": "off", "end_of_turn": "End of turn", "after_each_read": "After each read"}
        display = key_to_display.get(policy.get("prune_aggression", "off"), "off")
        idx = self.prune_aggression_combo.findText(display)
        self.prune_aggression_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.prune_aggression_combo.setToolTip(
            "When the model reads a file, edits it, and re-reads it, older reads and their "
            "file content are removed from context — only the newest read is kept."
        )
        form.addRow("Prune stale file reads:", self.prune_aggression_combo)

        self.delete_tool_keep_spin = QSpinBox()
        self.delete_tool_keep_spin.setRange(0, 20)
        self.delete_tool_keep_spin.setValue(policy.get("delete_tool_keep_count", 1))
        self.delete_tool_keep_spin.setSuffix(" per file/URL")
        form.addRow("Delete tool returns keep count:", self.delete_tool_keep_spin)

        note_continue = QLabel(
            "<i>The \"Continue\" button always extends the agent's last message via "
            "native assistant prefill (llama.cpp's --prefill-assistant, on by default). "
            "There is no mode to choose: nothing is injected into the prompt, the model "
            "simply keeps writing the text. If your server or model cannot prefill an "
            "open assistant turn — thinking models often can't — the button reports that "
            "plainly instead of falling back to a prompt trick.</i>"
        )
        note_continue.setWordWrap(True)
        note_continue.setStyleSheet("color:#8a8a8a;")
        form.addRow(note_continue)

        layout.addLayout(form)
        return tab

    # ---------- Web Access (incl. Fetch Limits) ----------

    def _build_web_tab(self, web_policy):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        layout.addWidget(QLabel("Web access level:"))
        self.web_mode_combo = QComboBox()
        self.web_mode_combo.addItems(["off", "whitelist_only", "search_and_fetch_safe", "tor", "open_web", "open_browser"])
        self.web_mode_combo.setCurrentText(web_policy.get("mode", "off"))
        layout.addWidget(self.web_mode_combo)

        # ---- Tor (only shown/used when mode == "tor") ----
        self.tor_host_edit = QLineEdit(web_policy.get("tor_socks_host", "127.0.0.1"))
        self.tor_host_edit.setToolTip(
            "SOCKS5 proxy host of your running Tor daemon. Default 127.0.0.1 (the "
            "standalone tor daemon's SocksPort). If you use Tor Browser itself "
            "(not the standalone daemon), its browser proxy port is usually 9150 "
            "rather than 9050 — set the port below accordingly."
        )
        self.tor_port_spin = QSpinBox()
        self.tor_port_spin.setRange(1, 65535)
        try:
            self.tor_port_spin.setValue(int(web_policy.get("tor_socks_port", 9050)))
        except (ValueError, TypeError):
            self.tor_port_spin.setValue(9050)
        self.tor_port_spin.setToolTip("SOCKS5 proxy port (default Tor daemon SocksPort is 9050; Tor Browser's browser proxy is 9150).")

        layout.addWidget(QLabel("Allowlist (one domain per line, used in whitelist_only mode):"))
        self.allowlist_edit = QPlainTextEdit("\n".join(web_policy.get("allowlist", [])))
        self.allowlist_edit.setMaximumHeight(70)
        layout.addWidget(self.allowlist_edit)

        layout.addWidget(QLabel("Blocklist (one domain per line, always enforced):"))
        self.blocklist_edit = QPlainTextEdit("\n".join(web_policy.get("blocklist", [])))
        self.blocklist_edit.setMaximumHeight(70)
        layout.addWidget(self.blocklist_edit)

        tor_group = QGroupBox("Tor (used only in 'tor' mode)")
        tor_form = QFormLayout(tor_group)
        tor_form.addRow("SOCKS5 host:", self.tor_host_edit)
        tor_form.addRow("SOCKS5 port:", self.tor_port_spin)
        tor_hint = QLabel(
            "<i>Requests run through this local Tor SOCKS5 proxy using socks5h "
            "(remote DNS), so your real IP and local DNS never leak to the origin "
            "server. Requires PySocks (pip install 'requests[socks]')."
        )
        tor_hint.setWordWrap(True)
        tor_hint.setStyleSheet("color:#8a8a8a; font-size:11px;")
        tor_form.addRow(tor_hint)
        layout.addWidget(tor_group)

        note = QLabel(
            "<i>off: no web tools. whitelist_only: fetch-only, restricted domains, no search. "
            "search_and_fetch_safe: DuckDuckGo HTML search + plain HTTP fetch. "
            "tor: same as search_and_fetch_safe but every request goes through your local Tor "
            "SOCKS5 proxy (no browser, no JS) for anonymity. "
            "open_web: headless Chromium (Playwright) — renders JS, bypasses more bot-detection. "
            "open_browser: your real Chrome via Selenium — full logged-in state, all JS. "
            "All modes enforce the blocklist.</i>"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        fetch_group = QGroupBox("Fetch limits (advanced)")
        fetch_form = QFormLayout(fetch_group)

        self.max_text_chars_spin = QSpinBox()
        self.max_text_chars_spin.setRange(1000, 100000)
        self.max_text_chars_spin.setSingleStep(500)
        self.max_text_chars_spin.setValue(self.web_fetch_policy_dict.get("max_text_chars", 8000))
        self.max_text_chars_spin.setSuffix(" characters")
        fetch_form.addRow("Max extracted text:", self.max_text_chars_spin)

        self.max_raw_bytes_spin = QSpinBox()
        self.max_raw_bytes_spin.setRange(50000, 10000000)
        self.max_raw_bytes_spin.setSingleStep(50000)
        self.max_raw_bytes_spin.setValue(self.web_fetch_policy_dict.get("max_raw_bytes", 500000))
        self.max_raw_bytes_spin.setSuffix(" bytes")
        fetch_form.addRow("Max raw download:", self.max_raw_bytes_spin)

        self.max_links_spin = QSpinBox()
        self.max_links_spin.setRange(0, 500)
        self.max_links_spin.setValue(self.web_fetch_policy_dict.get("max_links", 30))
        fetch_form.addRow("Max links:", self.max_links_spin)

        self.truncation_mode_combo = QComboBox()
        self.truncation_mode_combo.addItems(["show_popup", "deny", "silent"])
        current = self.web_fetch_policy_dict.get("truncation_mode", "show_popup")
        idx = self.truncation_mode_combo.findText(current)
        if idx >= 0:
            self.truncation_mode_combo.setCurrentIndex(idx)
        fetch_form.addRow("On truncation:", self.truncation_mode_combo)

        layout.addWidget(fetch_group)
        layout.addStretch()
        return tab

    # ---------- Tools & Safety (Command Whitelist + Script execution) ----------

    def _build_tools_safety_tab(self, policy):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        script_group = QGroupBox("Python script execution (run_python_script)")
        script_form = QFormLayout(script_group)

        self.script_run_mode_combo = QComboBox()
        self.script_run_mode_combo.addItems(["notify", "always_allow"])
        self.script_run_mode_combo.setCurrentText(policy.get("script_run_mode", "notify"))
        self.script_run_mode_combo.setToolTip(
            "notify: ask for approval before every script run.\n"
            "always_allow: run scripts without a prompt."
        )
        script_form.addRow("Approval mode:", self.script_run_mode_combo)

        self.script_timeout_spin = QSpinBox()
        self.script_timeout_spin.setRange(1, 3600)
        self.script_timeout_spin.setValue(policy.get("script_timeout", 10))
        self.script_timeout_spin.setSuffix(" s")
        script_form.addRow("Timeout:", self.script_timeout_spin)

        layout.addWidget(script_group)

        layout.addWidget(QLabel("Shell command whitelist:"))
        layout.addWidget(self.command_whitelist)
        return tab

    # ---------- Drafts & Time Barriers ----------

    def _build_drafts_time_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        draft_group = QGroupBox("Draft store (iterative edit staging)")
        draft_form = QFormLayout(draft_group)

        self.merge_gap_spin = QSpinBox()
        self.merge_gap_spin.setRange(1, 200)
        self.merge_gap_spin.setValue(self.draft_policy.get("merge_gap", 15))
        self.merge_gap_spin.setToolTip(
            "If two change regions are separated by fewer lines than this, they are merged "
            "into a single reviewable patch."
        )
        draft_form.addRow("Merge gap (lines):", self.merge_gap_spin)

        self.bulk_threshold_spin = QSpinBox()
        self.bulk_threshold_spin.setRange(1, 100)
        self.bulk_threshold_spin.setValue(int(self.draft_policy.get("bulk_threshold", 0.5) * 100))
        self.bulk_threshold_spin.setSuffix(" %")
        draft_form.addRow("Bulk threshold:", self.bulk_threshold_spin)

        layout.addWidget(draft_group)

        time_group = QGroupBox("Time-based auto-injection")
        time_layout = QVBoxLayout(time_group)

        intro = QLabel(
            "When the agent has been working on a task for a while, barriers inject a system "
            "message after each turn to inform the agent of elapsed time. Checked only after a "
            "turn completes — never mid-turn."
        )
        intro.setWordWrap(True)
        time_layout.addWidget(intro)

        self.time_barriers_enabled = QCheckBox("Enable time-based auto-injection")
        self.time_barriers_enabled.setChecked(self.time_barriers_policy.get("enabled", False))
        time_layout.addWidget(self.time_barriers_enabled)

        self.barrier_table = QTableWidget(0, 3)
        self.barrier_table.setHorizontalHeaderLabels(["Seconds", "Message", "Preview"])
        header = self.barrier_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.barrier_table.verticalHeader().setVisible(False)
        self.barrier_table.setShowGrid(False)
        self.barrier_table.setAlternatingRowColors(True)
        time_layout.addWidget(QLabel("Barriers (injection points):"))
        time_layout.addWidget(self.barrier_table)

        for b in self.time_barriers_policy.get("barriers", []):
            row = self.barrier_table.rowCount()
            self.barrier_table.insertRow(row)
            self.barrier_table.setItem(row, 0, QTableWidgetItem(str(b.get("seconds", 0))))
            self.barrier_table.setItem(row, 1, QTableWidgetItem(b.get("message", "")))
            preview_item = QTableWidgetItem(self._preview_barrier(b))
            preview_item.setForeground(QColor(140, 140, 140))
            self.barrier_table.setItem(row, 2, preview_item)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Barrier")
        add_btn.clicked.connect(self._add_barrier_row)
        remove_btn = QPushButton("- Remove Selected")
        remove_btn.clicked.connect(self._remove_barrier_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        time_layout.addLayout(btn_row)

        self.time_barriers_enabled.toggled.connect(lambda v: self.barrier_table.setEnabled(v))
        self.barrier_table.itemChanged.connect(self._on_barrier_changed)

        layout.addWidget(time_group)
        layout.addStretch()
        return tab

    def _preview_barrier(self, barrier: dict) -> str:
        secs = barrier.get("seconds", 0)
        if secs <= 0:
            return "(empty)"
        mins = secs // 60
        rem = secs % 60
        parts = []
        if mins:
            parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
        if rem:
            parts.append(f"{rem} second{'s' if rem != 1 else ''}")
        time_str = " and ".join(parts) if parts else "0 seconds"
        preview = f"[System information]: {time_str} have passed since your turn started."
        custom = barrier.get("message", "").strip()
        if custom:
            preview += "\n" + custom
        return preview

    def _add_barrier_row(self):
        row = self.barrier_table.rowCount()
        self.barrier_table.insertRow(row)
        self.barrier_table.setItem(row, 0, QTableWidgetItem("120"))
        self.barrier_table.setItem(row, 1, QTableWidgetItem(""))
        preview_item = QTableWidgetItem(self._preview_barrier({"seconds": 120}))
        preview_item.setForeground(QColor(140, 140, 140))
        self.barrier_table.setItem(row, 2, preview_item)
        self.barrier_table.selectRow(row)

    def _remove_barrier_row(self):
        row = self.barrier_table.currentRow()
        if row >= 0:
            self.barrier_table.removeRow(row)

    def _on_barrier_changed(self, item):
        row = item.row()
        col = item.column()
        if col in (0, 1):
            try:
                secs = int(self.barrier_table.item(row, 0).text() or "0")
            except ValueError:
                secs = 0
            msg = self.barrier_table.item(row, 1).text() if self.barrier_table.item(row, 1) else ""
            preview_item = QTableWidgetItem(self._preview_barrier({"seconds": secs, "message": msg}))
            preview_item.setForeground(QColor(140, 140, 140))
            self.barrier_table.setItem(row, 2, preview_item)

    # ---------- Behavior helpers ----------

    def _toggle_mode(self, managed: bool):
        for w in [self.binary_edit, self.model_edit, self.host_edit, self.port_spin,
                  self.context_spin, self.gpu_layers_spin, self.kv_k_combo, self.kv_v_combo,
                  self.flash_attn_combo, self.extra_args_edit]:
            w.setEnabled(managed)
        self.external_url_edit.setEnabled(not managed)
        # The subagent server is always app-managed (its own child process on its
        # own port), so its controls are independent of `managed`. Its tuning
        # knobs follow the subagent master switch instead — see
        # _toggle_subagent_mode, which deliberately leaves the model path row
        # editable so a model can be chosen before the feature is switched on.
        self._toggle_subagent_mode(self.subagent_enabled_checkbox.isChecked())

    def _toggle_subagent_mode(self, enabled: bool):
        """Gate the subagent server's *tuning* knobs on the master switch.

        The model path controls (subagent_model_edit / subagent_model_browse_btn)
        are intentionally absent from this list: they are how you satisfy
        `enabled`'s own precondition (a non-empty model_path), so greying them
        out while enabled=False would make the feature impossible to configure in
        the first place."""
        for w in [self.subagent_port_spin, self.subagent_context_spin,
                  self.subagent_device_combo, self.subagent_startup_spin,
                  self.subagent_binary_edit, self.subagent_extra_args_edit,
                  self.subagent_kv_k_combo, self.subagent_kv_v_combo]:
            w.setEnabled(enabled)

    def _copy_command(self):
        from PySide6.QtWidgets import QApplication
        current = self.get_config()
        args = current.build_args()
        command = current.binary_path + " " + " ".join(args)
        QApplication.clipboard().setText(command)

    # ---------- Getters ----------

    def get_config(self) -> ServerConfig:
        return ServerConfig(
            managed=self.managed_checkbox.isChecked(),
            binary_path=self.binary_edit.text().strip(),
            model_path=self.model_edit.text().strip(),
            mmproj_path=self.mmproj_edit.text().strip(),
            host=self.host_edit.text().strip() or "127.0.0.1",
            port=self.port_spin.value(),
            context_size=self.context_spin.value(),
            extra_args=self.extra_args_edit.text().strip(),
            external_base_url=self.external_url_edit.text().strip(),
            gpu_layers=self.gpu_layers_spin.value(),
            kv_cache_type_k=self.kv_k_combo.currentText(),
            kv_cache_type_v=self.kv_v_combo.currentText(),
            flash_attn=self.flash_attn_combo.currentText(),
            device=self.device_edit.text().strip(),
            split_mode=self.split_mode_combo.currentText(),
            tensor_split=self.tensor_split_edit.text().strip(),
            override_tensor=self.override_tensor_edit.text().strip(),
            cpu_moe=self.cpu_moe_checkbox.isChecked(),
            n_cpu_moe=self.n_cpu_moe_spin.value(),
            load_mode=self.load_mode_combo.currentText(),
            no_kv_offload=self.no_kv_offload_checkbox.isChecked(),
            numa_mode=self.numa_combo.currentText(),
            threads=self.threads_spin.value(),
            threads_batch=self.threads_batch_spin.value(),
            batch_size=self.batch_size_spin.value(),
            ubatch_size=self.ubatch_size_spin.value(),
            parallel_slots=self.parallel_spin.value(),
            cont_batching=self.cont_batching_checkbox.isChecked(),
            n_predict=self.n_predict_spin.value(),
            keep_tokens=self.keep_tokens_spin.value(),
            context_shift=self.context_shift_checkbox.isChecked(),
            rope_scaling=self.rope_scaling_combo.currentText(),
            rope_scale=self.rope_scale_spin.value(),
            rope_freq_base=self.rope_freq_base_spin.value(),
            rope_freq_scale=self.rope_freq_scale_spin.value(),
            yarn_orig_ctx=self.yarn_orig_ctx_spin.value(),
            reasoning_preserve=self.reasoning_preserve_checkbox.isChecked(),
            reasoning_format=self.reasoning_format_combo.currentText(),
            reasoning_mode=self.reasoning_mode_combo.currentText(),
            reasoning_budget=self.reasoning_budget_spin.value(),
            reasoning_budget_message=self.reasoning_budget_message_edit.text().strip(),
            temperature=self.temperature_spin.value(),
            top_k=self.top_k_spin.value(),
            top_p=self.top_p_spin.value(),
            min_p=self.min_p_spin.value(),
            typical_p=self.typical_p_spin.value(),
            repeat_last_n=self.repeat_last_n_spin.value(),
            repeat_penalty=self.repeat_penalty_spin.value(),
            presence_penalty=self.presence_penalty_spin.value(),
            frequency_penalty=self.frequency_penalty_spin.value(),
            dry_multiplier=self.dry_multiplier_spin.value(),
            dry_base=self.dry_base_spin.value(),
            dry_allowed_length=self.dry_allowed_length_spin.value(),
            dry_penalty_last_n=self.dry_penalty_last_n_spin.value(),
            mirostat=self.mirostat_combo.currentIndex(),
            mirostat_lr=self.mirostat_lr_spin.value(),
            mirostat_ent=self.mirostat_ent_spin.value(),
            seed=self.seed_spin.value(),
            lora_adapters=self._collect_lora_adapters(),
            api_key=self.api_key_edit.text().strip(),
            api_key_file=self.api_key_file_edit.text().strip(),
            cors_origins=self.cors_origins_edit.text().strip() or "*",
            enable_metrics=self.enable_metrics_checkbox.isChecked(),
            disable_webui=self.disable_webui_checkbox.isChecked(),
            disable_slots_endpoint=self.disable_slots_checkbox.isChecked(),
            speculative_type=self._selected_spec_types(),
            speculative_draft_n_max=self.spec_n_max_spin.value(),
            speculative_draft_n_min=self.spec_n_min_spin.value(),
            speculative_draft_model=self.spec_model_edit.text().strip(),
            speculative_draft_hf=self.spec_hf_edit.text().strip(),
        )

    def get_policy(self) -> dict:
        result = dict(self._original_policy)
        result.update({
            "system_prompt": self.system_prompt_edit.toPlainText(),
            "patch_mode": self.patch_mode_combo.currentText(),
            "batch_review_max": self.batch_max_spin.value(),
            "max_tool_rounds": self.max_rounds_spin.value(),
            "context_overflow_policy": self.overflow_combo.currentText(),
            "auto_expand_percent": self.expand_percent_spin.value(),
            "reasoning_retention": self.reasoning_combo.currentText(),
            "chat_render_mode": self.render_combo.currentText(),
            "force_wrap": self.force_wrap_checkbox.isChecked(),
            "auto_scroll_to_bottom": self.auto_scroll_checkbox.isChecked(),
            "stream_to_chat": self.stream_to_chat_checkbox.isChecked(),
            "enable_thinking": self.enable_thinking_checkbox.isChecked(),
            "preserve_thinking": self.preserve_thinking_checkbox.isChecked(),
            "prune_aggression": self.prune_aggression_combo.currentText().lower().replace(" ", "_"),
            "delete_tool_keep_count": self.delete_tool_keep_spin.value(),
            "script_run_mode": self.script_run_mode_combo.currentText(),
            "script_timeout": self.script_timeout_spin.value(),
        })
        return result

    def get_draft_policy(self) -> dict:
        result = dict(self._original_draft_policy)
        result.update({
            "merge_gap": self.merge_gap_spin.value(),
            "bulk_threshold": self.bulk_threshold_spin.value() / 100,
        })
        return result

    def get_subagent_policy(self) -> dict:
        """Return the subagent model-server settings as a plain dict, started
        from the SubagentPolicy the dialog was opened with so any unknown/
        forward-compat key survives a save (same merge discipline as
        get_policy()). `device` is read from the combo's stored data, not its
        display text, so rewording a label can never corrupt the value."""
        result = self.subagent_policy.to_dict()
        result.update({
            "enabled": self.subagent_enabled_checkbox.isChecked(),
            "model_path": self.subagent_model_edit.text().strip(),
            "binary_path": self.subagent_binary_edit.text().strip(),
            "port": self.subagent_port_spin.value(),
            "context_size": self.subagent_context_spin.value(),
            "device": self.subagent_device_combo.currentData() or "gpu",
            "startup_timeout": self.subagent_startup_spin.value(),
            "extra_args": self.subagent_extra_args_edit.text().strip(),
            # currentData() is "" for the "inherit" entry — the sentinel
            # build_server_config() understands — and a KV_CACHE_TYPES member
            # otherwise. Never the display label.
            "kv_cache_type_k": self.subagent_kv_k_combo.currentData() or "",
            "kv_cache_type_v": self.subagent_kv_v_combo.currentData() or "",
            # Read per deployment (chat_template_kwargs), not by the server.
            "enable_thinking": self.subagent_enable_thinking_checkbox.isChecked(),
            "preserve_thinking": self.subagent_preserve_thinking_checkbox.isChecked(),
        })
        return result

    def get_vision_policy(self) -> dict:
        """Compatibility shim: the Vision tab (mmproj mode selector + caption
        fallback) has been removed. mmproj path lives on the Backend tab and
        is read directly from server_config.mmproj_path elsewhere; this
        always reports vision as effectively disabled-fallback so main_window
        doesn't try to build a captioner that no longer has a UI."""
        result = dict(self._original_vision_policy)
        result.update({
            "mode": "mmproj_only",
            "mmproj_path": self.mmproj_edit.text().strip(),
            "caption_fallback_enabled": False,
        })
        return result

    def done(self, result):
        super().done(result)

    def get_web_policy(self) -> dict:
        result = dict(self._original_web_policy)
        allow = [l.strip() for l in self.allowlist_edit.toPlainText().splitlines() if l.strip()]
        block = [l.strip() for l in self.blocklist_edit.toPlainText().splitlines() if l.strip()]
        result.update({"mode": self.web_mode_combo.currentText(), "allowlist": allow, "blocklist": block})
        result["tor_socks_host"] = self.tor_host_edit.text().strip() or "127.0.0.1"
        result["tor_socks_port"] = self.tor_port_spin.value()
        return result

    def get_web_fetch_policy(self) -> dict:
        result = dict(self._original_web_fetch_policy)
        result.update({
            "max_text_chars": self.max_text_chars_spin.value(),
            "max_raw_bytes": self.max_raw_bytes_spin.value(),
            "max_links": self.max_links_spin.value(),
            "truncation_mode": self.truncation_mode_combo.currentText(),
        })
        return result

    def get_command_policy(self) -> dict:
        return self.command_whitelist.get_policy()

    def get_time_barriers_policy(self) -> dict:
        barriers = []
        enabled = self.time_barriers_enabled.isChecked()
        for row in range(self.barrier_table.rowCount()):
            sec_item = self.barrier_table.item(row, 0)
            msg_item = self.barrier_table.item(row, 1)
            if sec_item:
                try:
                    secs = int(sec_item.text())
                except ValueError:
                    secs = 0
            else:
                continue
            msg = msg_item.text() if msg_item else ""
            if secs > 0:
                barriers.append({"seconds": secs, "message": msg})
        barriers.sort(key=lambda b: b["seconds"])
        result = dict(self._original_time_barriers_policy)
        result.update({"enabled": enabled, "barriers": barriers})
        return result