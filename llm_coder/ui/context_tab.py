"""
Context tab: shows token usage for the main conversation (and, when a subagent
model is configured, for the most recent subagent deployment), compaction
strategy toggle buttons, manual compaction trigger, summary view, and raw
context display.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTextEdit, QGroupBox, QButtonGroup, QRadioButton, QSpinBox
)
from PySide6.QtCore import Qt

# Colour thresholds for both meters: yellow from 75% of the window, red from 90%.
_WARN_PCT = 75
_CRIT_PCT = 90
_COLOR_OK = "#3fa34d"
_COLOR_WARN = "#d19900"
_COLOR_CRIT = "#e05a5a"


def _bar_color(pct: int) -> str:
    if pct >= _CRIT_PCT:
        return _COLOR_CRIT
    if pct >= _WARN_PCT:
        return _COLOR_WARN
    return _COLOR_OK


class ContextTab(QWidget):
    def __init__(self, agent_provider, on_policy_changed, on_compact_requested=None,
                 subagent_enabled_provider=None, parent=None):
        super().__init__(parent)
        self.agent_provider = agent_provider
        self.on_policy_changed = on_policy_changed
        self.on_compact_requested = on_compact_requested or (lambda strategy: None)
        # Whether a subagent model is configured+enabled. Defaults to asking the
        # agent itself, so the tab still works if the caller passes nothing.
        self.subagent_enabled_provider = subagent_enabled_provider or (
            lambda: bool(getattr(self.agent_provider(), "subagent_enabled", lambda: False)()))
        layout = QVBoxLayout(self)

        # --- Usage section ---
        layout.addWidget(QLabel("Main context usage:"))
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        layout.addWidget(self.bar)

        self.usage_label = QLabel("No active session.")
        self.usage_label.setStyleSheet("color:#8a8a8a;")
        layout.addWidget(self.usage_label)

        # --- Subagent usage section ---
        # Hidden unless a subagent model is configured and enabled, so a user
        # who never turned the feature on sees exactly one meter as before.
        self.subagent_group = QWidget()
        sg_layout = QVBoxLayout(self.subagent_group)
        sg_layout.setContentsMargins(0, 0, 0, 0)
        sg_layout.addWidget(QLabel("Subagent context usage (last deployment):"))
        self.subagent_bar = QProgressBar()
        self.subagent_bar.setRange(0, 100)
        sg_layout.addWidget(self.subagent_bar)
        self.subagent_usage_label = QLabel("No subagent deployment yet.")
        self.subagent_usage_label.setStyleSheet("color:#8a8a8a;")
        sg_layout.addWidget(self.subagent_usage_label)
        layout.addWidget(self.subagent_group)
        self.subagent_group.setVisible(False)

        # --- Compaction strategy ---
        strategy_group = QGroupBox("Compaction strategy:")
        strategy_layout = QVBoxLayout(strategy_group)

        self.button_group = QButtonGroup(self)
        self.btn_delete = QRadioButton("Delete tool call returns")
        self.btn_delete.setToolTip(
            "Remove old tool result messages from context (keep count below).\n\n"
            "0 = remove ALL tool results. "
            "1 = keep the latest result per file/URL. "
            "Higher values keep more recent results."
        )
        self.button_group.addButton(self.btn_delete)
        self.btn_delete.setChecked(True)
        strategy_layout.addWidget(self.btn_delete)

        # Keep-count spinbox for delete strategy
        keep_row = QHBoxLayout()
        keep_label = QLabel("Keep last:")
        keep_label.setStyleSheet("color:#8a8a8a;")
        keep_spin = QSpinBox()
        keep_spin.setRange(0, 20)
        keep_spin.setValue(1)
        keep_spin.setSuffix(" per file/URL")
        keep_spin.setToolTip("Number of most recent tool results to keep per unique key "
                             "(e.g. per file for read_file, per URL for fetch_text_url). "
                             "0 = remove all.")
        keep_spin.setAlignment(Qt.AlignCenter)
        keep_row.addWidget(keep_label)
        keep_row.addWidget(keep_spin)
        keep_row.addStretch()
        self._keep_spin = keep_spin
        strategy_layout.addLayout(keep_row)

        self.btn_summarize = QRadioButton("Summarize conversation")
        self.btn_summarize.setToolTip("Replace entire conversation with a model-generated summary, "
                                       "including key files, actions, and interaction narrative.")
        self.button_group.addButton(self.btn_summarize)
        strategy_layout.addWidget(self.btn_summarize)

        layout.addWidget(strategy_group)

        # --- Compact button ---
        btn_row = QHBoxLayout()
        self.compact_btn = QPushButton("Compact Now")
        self.compact_btn.clicked.connect(self._compact_now)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(self.compact_btn)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- Summary view ---
        layout.addWidget(QLabel("Last compaction summary:"))
        self.summary_view = QTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMaximumHeight(80)
        layout.addWidget(self.summary_view)

        # --- Raw context ---
        layout.addWidget(QLabel("Raw context (exact messages currently sent to the model):"))
        self.raw_view = QTextEdit()
        self.raw_view.setReadOnly(True)
        font = self.raw_view.font()
        font.setFamily("monospace")
        self.raw_view.setFont(font)
        # Track whether the user is at the bottom so refreshes can preserve
        # their scroll position instead of forcing the view back to the top.
        self._raw_follow_bottom = False
        self.raw_view.verticalScrollBar().valueChanged.connect(self._on_raw_scroll)
        layout.addWidget(self.raw_view, stretch=1)

        self.refresh()

    def set_policy_value(self, value: str):
        """No-op — overflow policy is gone, kept for backwards compat."""
        pass

    def get_keep_count(self) -> int:
        """Return the current keep count setting from the spinbox."""
        return self._keep_spin.value()

    def set_keep_count(self, value: int):
        """Set the keep count spinbox value (used when loading from settings)."""
        self._keep_spin.setValue(value)

    def _compact_now(self):
        agent = self.agent_provider()
        if agent is None:
            return
        self.compact_btn.setEnabled(False)
        if self.btn_delete.isChecked():
            strategy = "delete_tool_returns"
        elif self.btn_summarize.isChecked():
            strategy = "summarize_conversation"
        else:
            strategy = "delete_tool_returns"
        self.on_compact_requested(strategy, self._keep_spin.value())

    def set_compaction_result(self, summary: str):
        self.summary_view.setPlainText(summary)
        self.compact_btn.setEnabled(True)
        self.refresh()

    def refresh(self):
        agent = self.agent_provider()
        if agent is None:
            self.usage_label.setText("No active session.")
            self.bar.setValue(0)
            self._refresh_subagent_view(None)
            return
        real_tokens = agent.current_context_tokens_real() if hasattr(agent, "current_context_tokens_real") else None
        heuristic_tokens = agent.current_context_tokens()
        # Use the larger of real and heuristic: real_tokens can be stale
        # (set during streaming from the *old* context before tool-call
        # results were added to messages), so the heuristic is needed as a
        # floor to avoid the meter jumping down when a file_read result is
        # added between streaming chunks.
        tokens = max(heuristic_tokens, real_tokens) if real_tokens is not None else heuristic_tokens
        limit = agent.context_limit
        pct = min(100, int(100 * tokens / max(1, limit)))
        self.bar.setValue(pct)
        self.usage_label.setText(f"{tokens:,} / {limit:,} tokens ({pct}%)")
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {_bar_color(pct)}; }}")

        self._refresh_subagent_view(agent)
        self._refresh_raw_view(agent)

    def _refresh_subagent_view(self, agent):
        """Meter for the subagent model's own context window.

        A deployment is a fresh window every time (SubagentRunner.run() builds
        its transcript from scratch), so this is the peak occupancy of the most
        recent deployment — not a running total across deployments. Shown only
        when a subagent model is configured and enabled.
        """
        try:
            enabled = bool(self.subagent_enabled_provider())
        except Exception:
            enabled = False
        if not enabled:
            # Keep the values but hide the whole block: re-showing it after a
            # settings change should not flash a stale reading mid-refresh.
            self.subagent_group.setVisible(False)
            return
        self.subagent_group.setVisible(True)
        if agent is None or not getattr(agent, "subagent_deployments", 0):
            self.subagent_bar.setValue(0)
            self.subagent_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background-color: {_COLOR_OK}; }}")
            self.subagent_usage_label.setText("No subagent deployment yet.")
            self.subagent_usage_label.setStyleSheet("color:#8a8a8a;")
            return

        tokens = int(getattr(agent, "subagent_last_context_tokens", 0) or 0)
        # The limit comes from the deployment itself when the runner reported
        # one; otherwise fall back to the current policy, which is the same
        # number whenever the child has not been restarted with a new ctx.
        limit = int(getattr(agent, "subagent_last_context_limit", 0) or 0) \
            or (agent.subagent_context_limit() if hasattr(agent, "subagent_context_limit") else 0)
        if limit <= 0:
            self.subagent_bar.setValue(0)
            self.subagent_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background-color: {_COLOR_OK}; }}")
            self.subagent_usage_label.setText(f"{tokens:,} tokens used (window size unknown)")
            self.subagent_usage_label.setStyleSheet("color:#8a8a8a;")
            return

        pct = min(100, int(100 * tokens / limit))
        self.subagent_bar.setValue(pct)
        self.subagent_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {_bar_color(pct)}; }}")
        n = int(getattr(agent, "subagent_deployments", 0) or 0)
        running = self._subagent_running(agent)
        state = "current deployment" if running else f"deployment #{n}"
        self.subagent_usage_label.setText(
            f"{tokens:,} / {limit:,} tokens ({pct}%) — peak, {state}")
        self.subagent_usage_label.setStyleSheet("color:#8a8a8a;")

    def _subagent_running(self, agent) -> bool:
        """True while a subagent deployment is in flight.

        Uses the worker thread's own liveness flag (Agent._deploy_subagent sets
        it just before SubagentRunner.run() and clears it in its finally block),
        falling back to the last reported activity state — the activity callback
        is only wired to the UI's signal while a turn is running, so the flag is
        the reliable one and this is merely a belt for the belt."""
        if bool(getattr(agent, "_subagent_deploy_active", False)):
            return True
        state = getattr(agent, "_last_activity_state", "") or ""
        return state.startswith("subagent")

    def _on_raw_scroll(self, value: int):
        """Track whether the raw view is scrolled to the bottom."""
        bar = self.raw_view.verticalScrollBar()
        self._raw_follow_bottom = value >= bar.maximum() - 2

    def _refresh_raw_view(self, agent):
        def _flatten(content):
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif p.get("type") == "image_url":
                        parts.append("[image attachment]")
                return "\n".join(parts)
            return str(content)

        lines = []
        for i, m in enumerate(agent.messages):
            role = m.get("role", "?")
            text = _flatten(m.get("content"))
            tool_calls = m.get("tool_calls")
            header = f"--- [{i}] {role.upper()} ---"
            lines.append(header)
            if text:
                lines.append(text)
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    lines.append(f"  tool_call: {fn.get('name')}({fn.get('arguments')})")
        lines.append("")
        new_text = "\n".join(lines)
        # Only rewrite the view if the content actually changed; otherwise
        # refreshes during inference would keep resetting the scroll position.
        if new_text != self.raw_view.toPlainText():
            bar = self.raw_view.verticalScrollBar()
            was_following = self._raw_follow_bottom
            old_max = bar.maximum()
            old_value = bar.value()
            self.raw_view.setPlainText(new_text)
            # Restore the scroll position so the user is never force-scrolled
            # back to the top. If they were following the bottom, keep them
            # there; otherwise preserve their current vertical offset.
            if was_following:
                bar.setValue(bar.maximum())
            else:
                frac = (old_value / old_max) if old_max else 0.0
                bar.setValue(int(frac * bar.maximum()))
