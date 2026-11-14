"""
Main window. Chat rendering now delegates entirely to ui.chat_panel.ChatPanel
and core.chat_message.ChatMessage — see those modules for the "why". This
file no longer contains ANY raw-HTML chat surgery: no toHtml()/setHtml()
round-tripping, no regex div-searching, no shared _chat_history dict-list.

Removed entirely vs. the previous version (superseded by ChatPanel):
    _append_chat, _render_entry, _rerender_chat, _append_live_chat_text,
    _close_stream_div, _format_assistant_html, _replace_last_assistant_div,
    _re_render_live_response, _rerender_restored_messages
Removed state (now owned by ChatMessage/ChatPanel):
    _chat_history, _live_response_text, _live_reasoning_text,
    _live_response_rerendered, _stream_div_opened, _tool_call_opened,
    _had_live_content, _streaming_to_chat (still exists but only gates
    behavior, not a buffer)
"""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTextEdit,
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QLabel, QFileDialog, QMessageBox,
    QScrollArea, QTabWidget, QComboBox, QInputDialog, QFrame, QMenu, QDialog,
    QDialogButtonBox, QGroupBox, QSizePolicy, QRadioButton, QCheckBox, QButtonGroup
)
import json
import os
from PySide6.QtCore import Qt, QTimer, Signal, QSize, QRect
from PySide6.QtGui import QColor, QFont

from core.workspace import Workspace, WorkspaceError
from core.snapshots import SnapshotStore
from core.tools import ToolExecutor, PendingPatch
from core.llm_client import LLMClient
from core.agent import Agent, AgentPolicy, DEFAULT_SYSTEM_PROMPT
from core.agent_worker import AgentWorker, CompactionWorker
from core.llama_runtime import LlamaServerRuntime, ServerConfig, probe_endpoint
from core.app_settings import AppSettings
from core.subagent_server import SubagentPolicy, SubagentServerRuntime
from core.draft_store import DraftStore
from core.chat_message import ChatMessage, VERBOSITY_LABELS
from core import edit_tracking

from ui.settings_dialog import SettingsDialog
from ui.diff_viewer import PatchReviewPanel
from ui.history_dialog import HistoryDialog
from ui.tools_tab import ToolsTab
from ui.context_tab import ContextTab
from ui.chat_panel import ChatPanel

from core.session_manager import SessionManager
from ui.overflow_dialog import OverflowChoiceDialog
from ui.server_detect_dialog import ServerDetectDialog
from ui.web_truncation_dialog import WebFetchTruncationDialog
from ui.web_browser_kill_dialog import WebBrowserKillDialog
from core.browser_fetcher import close_browser as _close_browser


ACTIVITY_LABELS = {
    "idle": ("Idle", "#8a8a8a"),
    "waiting_for_model": ("Reading...", "#5591c7"),
    "compacting": ("Compacting history...", "#d19900"),
}


def _short(payload: dict) -> str:
    """Module-level helper — not a method, so no self needed when called.
    No longer truncates: tool_call/tool_result messages are collapsible by
    default (see chat_message.py), so the collapse UI is what hides bulk
    now, not lossy truncation baked into the text itself."""
    if not payload:
        return ""
    parts = [f"{k}={v}" for k, v in payload.items()]
    return ", ".join(parts)


def _format_tool_args(payload: dict) -> str:
    """Format tool arguments for display — avoid raw JSON, use readable key=value pairs."""
    return _short(payload)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


class ChatInputEdit(QTextEdit):
    send_requested = Signal()
    files_dropped = Signal(list)
    image_pasted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAcceptRichText(False)

    def insertFromMimeData(self, source):
        if source.hasImage():
            import tempfile, os, time
            from PySide6.QtGui import QImage
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                tmp_dir = tempfile.gettempdir()
                fname = f"clipboard_paste_{int(time.time() * 1000)}.png"
                path = os.path.join(tmp_dir, fname)
                img.save(path, "PNG")
                self.image_pasted.emit(path)
                return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class AttachmentChip(QFrame):
    removed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.setStyleSheet(
            "QFrame { background:#2a2a2a; border:1px solid #444; border-radius:10px; padding:2px 4px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        import os as _os
        name = _os.path.basename(path)
        icon = "🖼" if _os.path.splitext(path)[1].lower() in IMAGE_EXTS else "📄"
        label = QLabel(f"{icon} {name[:28]}")
        label.setStyleSheet("color:#e6e6e6; font-size:11px;")
        remove_btn = QPushButton("×")
        remove_btn.setFixedSize(24, 24)
        remove_btn.setStyleSheet(
            "QPushButton { border:none; color:#e05a5a; font-weight:bold; font-size:14px; border-radius:12px; }"
            "QPushButton:hover { background:#3a2020; }"
        )
        remove_btn.clicked.connect(lambda: self.removed.emit(self.path))
        layout.addWidget(label)
        layout.addWidget(remove_btn)


class _WrappedChoiceMixin:
    """Word-wrap for QRadioButton/QCheckBox.

    Qt has no word-wrap API for QAbstractButton at all: there is no
    setWordWrap() (calling it raises AttributeError), QStyleOptionButton has
    no WordWrap feature flag, and this is tracked as QTBUG-5370 — an open,
    unimplemented feature request since 2009. So it has to be emulated.

    Approach (ported from the accepted PyQt workaround on QTBUG-5370): fold
    the text to the width the widget actually has, in resizeEvent(), and make
    the layout give us that width by dropping the horizontal size policy from
    Expanding to Preferred. Expanding is what makes naive folding fail — the
    layout keeps granting the button its full single-line width hint, so the
    measured width is never narrow enough to fold anything.

    The indicator's real width comes from style().itemTextRect() rather than
    a guessed constant, so it stays correct across platforms/styles.

    The pristine string lives in ``choice_text``; text() carries the injected
    newlines and must never be sent back to the model.
    """

    def _init_wrapped(self, text: str):
        self._raw_text = text or ""
        self._wrapping = False
        super().setText(self._raw_text)
        # Essential: without this the layout keeps the one-line width hint.
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        policy.setVerticalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)
        self.updateGeometry()

    def _indicator_width(self) -> int:
        """Width taken by the radio/checkbox indicator + spacing, i.e. total
        widget width minus the width the style would give to our text."""
        text_w = self.style().itemTextRect(
            self.fontMetrics(), QRect(),
            int(Qt.TextFlag.TextShowMnemonic), False, self.text(),
        ).width()
        return max(0, self.sizeHint().width() - text_w)

    def _wrap_lines(self, width: int):
        if self._wrapping or width <= 0:
            return
        self._wrapping = True
        try:
            fm = self.fontMetrics()
            result, line, word = "", "", ""
            for ch in (self._raw_text.replace("\n", " ") + " "):
                word += ch
                if not ch.isspace():
                    continue
                if line and fm.horizontalAdvance(line + word.strip()) > width:
                    result += line.strip() + "\n"
                    line = word
                else:
                    line += word
                word = ""
            result += line.strip()
            super().setText(result.strip())
        finally:
            self._wrapping = False

    # --- public surface -----------------------------------------------------
    @property
    def choice_text(self) -> str:
        """The original, newline-free option text — always read this back,
        never text()."""
        return self._raw_text

    # --- Qt overrides -------------------------------------------------------
    def resizeEvent(self, event):
        self._wrap_lines(event.size().width() - self._indicator_width())
        super().resizeEvent(event)

    def minimumSizeHint(self):
        # Borrow the native *width* but report the wrapped height; otherwise
        # the layout hands us a single clipped line.
        hint = super().minimumSizeHint()
        wrapped = super().sizeHint()
        return QSize(hint.width(), max(hint.height(), wrapped.height()))

    def setText(self, text: str):
        self._raw_text = text or ""
        super().setText(self._raw_text)
        self._wrap_lines(self.width() - self._indicator_width())


class WrappedRadioButton(_WrappedChoiceMixin, QRadioButton):
    def __init__(self, text: str = "", parent=None):
        # Pass "" to Qt so the unwrapped single-line width hint never reaches
        # the layout; _init_wrapped installs the text afterwards.
        super().__init__("", parent)
        self._init_wrapped(text)


class WrappedCheckBox(_WrappedChoiceMixin, QCheckBox):
    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._init_wrapped(text)


class _AskUserDialog(QDialog):
    """Custom dialog for ask_user with constrained width and word-wrapping.

    Replaces the static QInputDialog.getText/getItem calls to prevent
    the dialog from stretching horizontally when the question text is long.
    The dialog is resizable so height grows/shrinks with wrapped content.

    Three input modes, chosen by the arguments the model passed:
      * no choices                -> a single free-text line
      * choices, multi_select off -> radio buttons (pick exactly one)
      * choices, multi_select on  -> checkboxes (pick any number)

    A free-text "Other" field is ALWAYS available — it is an implicit extra
    option, independent of the allow_custom_answer flag — so the user is never
    trapped by the option set the model happened to list. In multi-select mode
    typed text is simply appended to the ticked choices rather than replacing
    them, so "PostgreSQL, plus whatever keeps the old tests green" is
    expressible.
    """
    MAX_WIDTH = 600

    def __init__(self, question: str, choices: list, allow_custom: bool,
                 multi_select: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agent needs input")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowCloseButtonHint)
        self.setMaximumWidth(self.MAX_WIDTH)
        self.setMinimumWidth(300)
        # Start at the widest allowed size. The buttons fold in resizeEvent, so
        # giving them a real width up front means the very first layout pass
        # wraps them instead of showing one clipped line until the user
        # resizes. Resizable, so re-folding tracks the user's width.
        self.resize(self.MAX_WIDTH, 200)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Question label with word wrapping
        question_label = QLabel(question)
        question_label.setWordWrap(True)
        question_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(question_label)

        self.result_text = ""
        self.multi_select = bool(multi_select) and bool(choices)

        if choices:
            # A button group keeps the choice widgets mutually exclusive in
            # single mode; in multi mode each checkbox stands on its own.
            self._group = QButtonGroup(self)
            self._group.setExclusive(not self.multi_select)
            self._buttons = []
            for text in choices:
                btn = (WrappedCheckBox(text) if self.multi_select
                       else WrappedRadioButton(text))
                btn.setStyleSheet("spacing: 8px;")
                # No Expanding horizontal policy here on purpose: the button
                # must be allowed to shrink below its single-line width hint,
                # otherwise resizeEvent never receives a narrow-enough width to
                # fold into and the text stays clipped. The mixin already sets
                # both policies to Preferred.
                self._group.addButton(btn)
                layout.addWidget(btn)
                self._buttons.append(btn)
            # A hint so the user knows whether one or many are expected.
            hint = QLabel("(select all that apply)" if self.multi_select else "(pick one)")
            hint.setStyleSheet("color:#888888; font-size:11px; font-style:italic;")
            layout.addWidget(hint)
        else:
            self._group = None
            self._buttons = []

        # Free-text "Other" — implicit and always present.
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("Other — type your own answer…")
        layout.addWidget(self.custom_edit)

        # OK / Cancel buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self.custom_edit.returnPressed.connect(self._on_accept)

        # Without these the dialog can open behind the main window (notably on
        # X11/multi-monitor), which looks identical to "nothing happened".
        self.setModal(True)
        self.raise_()
        self.activateWindow()

    def _on_accept(self):
        custom = self.custom_edit.text().strip()
        if not self._buttons:
            # Free-form mode: whatever was typed is the whole answer.
            self.result_text = custom
        elif self.multi_select:
            # Chosen checkboxes first (in the order the model listed them),
            # then any typed text as an additional option.
            picked = [b.choice_text for b in self._buttons if b.isChecked()]
            if custom:
                picked.append(custom)
            self.result_text = ", ".join(picked)
        else:
            # Single mode: a typed custom answer wins over the radio, so the
            # user can override the list without hunting for an "Other" item.
            checked = next((b for b in self._buttons if b.isChecked()), None)
            self.result_text = custom or (checked.choice_text if checked else "")
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LLM Coder — Local Agentic Coding Assistant")
        self.resize(1300, 850)

        self.app_settings = AppSettings()
        self.server_config = self.app_settings.load_server_config()
        self.agent_policy_dict = self.app_settings.load_agent_policy()
        if "prune_aggression" not in self.agent_policy_dict:
            self.agent_policy_dict["prune_aggression"] = self.app_settings.load_prune_aggression()
            self.app_settings.save_agent_policy(self.agent_policy_dict)
        if "delete_tool_keep_count" not in self.agent_policy_dict:
            self.agent_policy_dict["delete_tool_keep_count"] = self.app_settings.load_delete_tool_keep_count()
            self.app_settings.save_agent_policy(self.agent_policy_dict)
        self.draft_policy_dict = self.app_settings.load_draft_policy()
        self.tool_toggles = self.app_settings.load_tool_toggles()
        self.subagent_tool_toggles = self.app_settings.load_subagent_tool_toggles()
        self.web_policy_dict = self.app_settings.load_web_policy()
        self.web_fetch_policy_dict = self.app_settings.load_web_fetch_policy()
        self.vision_policy_dict = self.app_settings.load_vision_policy()
        self.subagent_policy_dict = self.app_settings.load_subagent_policy()
        self.command_policy_dict = self.app_settings.load_command_policy()
        self.time_barriers_policy = self.app_settings.load_time_barriers()

        self.runtime = LlamaServerRuntime()
        self.runtime.log_line.connect(self._append_log)
        self.runtime.state_changed.connect(self._on_server_state)
        self.runtime.starting_phase.connect(self._on_server_phase)
        self.runtime.perf_metrics.connect(self._on_perf_metrics)
        self.runtime.prompt_progress.connect(self._on_prompt_progress)
        # Latest live prompt-processing progress reported by llama-server
        # (see the "Server Log" tab print_timing lines). Used to render
        # "Reading... (x%)" in the activity label while the server prefills.
        self._prompt_progress = None  # float 0.0-1.0, or None when unknown
        self._initial_prompt_progress = None
        self._in_reading_state = False

        # ---- Subagent model: an independent llama-server child ----
        # Built here but never started from __init__. SubagentServerRuntime.start()
        # runs either from _start_server (so it comes up alongside the main model)
        # or on the first deployment. A half-configured or disabled policy
        # therefore costs no RAM and no startup time.
        self.subagent_server = SubagentServerRuntime(SubagentPolicy.from_dict(self.subagent_policy_dict))

        self.workspace: Workspace | None = None
        self.snapshot_store: SnapshotStore | None = None
        self.session_manager: SessionManager | None = None
        self.draft_store: DraftStore | None = None
        self.executor: ToolExecutor | None = None
        self.agent: Agent | None = None
        self.worker: AgentWorker | None = None
        self._pending_prompt_summary = "agent change"

        # Streaming state: only the CURRENT in-flight message objects need
        # tracking (there is no shared text buffer anymore — see chat_panel.py).
        self._live_content_msg: ChatMessage | None = None
        self._live_reasoning_msg: ChatMessage | None = None
        # Live bubbles belonging to the currently-deployed subagent (its own
        # prose / thinking / streamed tool args). Kept separate from the
        # parent's refs above so a worker can never append to the agent's
        # bubble — see _append_subagent_delta and the "subagent_begin"/
        # "subagent_end" markers handled in _on_delta.
        self._live_subagent_content_msg: ChatMessage | None = None
        self._live_subagent_reasoning_msg: ChatMessage | None = None
        self._live_subagent_tool_msg: ChatMessage | None = None
        # True between a "subagent_begin" and its matching "subagent_end". Used
        # to force the parent's first post-deployment delta into a new bubble
        # even when the round number did not change.
        self._subagent_block_open = False
        self._streaming_to_chat = True
        # Per-round flags: track whether THIS round streamed reasoning/content
        # live, independent of the _live_*_msg refs (which get cleared to None
        # by finalize at the top of _on_step_finished). Checking the cleared
        # refs later in the same method was the bug — they're always None by
        # then, so the "was it already streamed?" guard was always false,
        # duplicating the final round's reasoning every time.
        self._had_live_reasoning_this_round = False
        self._had_live_content_this_round = False
        # Set when end_turn's summary has already been displayed via the
        # synchronous tool_event callback (fires DURING step(), before
        # _on_step_finished ever runs). result.assistant_text is set to
        # that same summary text when visible_text is empty, so without
        # this flag the fallback-append below re-displays it a second time.
        self._end_turn_summary_shown = False

        self._build_ui()

        geometry = self.app_settings.load_geometry()
        if geometry:
            self.restoreGeometry(geometry)

        last_project = self.app_settings.load_last_project()
        if last_project:
            self._load_project(last_project)

        QTimer.singleShot(400, self._check_existing_server)

        self._context_refresh_timer = QTimer(self)
        self._context_refresh_timer.setInterval(500)
        self._context_refresh_timer.timeout.connect(self._periodic_context_refresh)
        self._context_refresh_timer.start()

    # ---------- UI construction ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        self.project_label = QLabel("No project open")
        open_btn = QPushButton("Open Project")
        open_dir_btn = QPushButton("Open Directory")
        settings_btn = QPushButton("Settings")
        history_btn = QPushButton("File History")
        new_session_btn = QPushButton("+ New Session")
        new_session_btn.setFixedWidth(110)
        new_session_btn.setStyleSheet(
            "QPushButton { "
            "  background: #2a5a2a; "
            "  color: #cdeecb; "
            "  border: 1px solid #3a7a3a; "
            "  border-radius: 4px; "
            "  padding: 4px 8px; "
            "  font-weight: bold; "
            "  font-size: 12px; "
            "} "
            "QPushButton:hover { "
            "  background: #3a7a3a; "
            "}"
        )
        start_btn = QPushButton("Start Server")
        stop_btn = QPushButton("Stop Server")
        open_btn.clicked.connect(self._open_project)
        open_dir_btn.clicked.connect(self._open_project_directory)
        settings_btn.clicked.connect(self._open_settings)
        history_btn.clicked.connect(self._open_history)
        new_session_btn.clicked.connect(self._new_session)
        start_btn.clicked.connect(self._start_server)
        stop_btn.clicked.connect(self._stop_server)

        top_bar.addWidget(self.project_label)
        top_bar.addStretch()
        top_bar.addWidget(new_session_btn)
        top_bar.addWidget(open_dir_btn)
        top_bar.addWidget(history_btn)
        top_bar.addWidget(settings_btn)
        top_bar.addWidget(start_btn)
        top_bar.addWidget(stop_btn)
        top_bar.addWidget(open_btn)
        root_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        left = QWidget()
        left.setStyleSheet("background: #1a1a1a;")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(4)

        session_header = QHBoxLayout()
        session_header.setContentsMargins(0, 0, 0, 0)
        session_label = QLabel("Sessions")
        session_label.setStyleSheet("color: #999999; font-size: 11px; font-weight: bold;")
        session_header.addWidget(session_label)
        session_header.addStretch()
        refresh_sessions_btn = QPushButton("⟳")
        refresh_sessions_btn.setFixedSize(24, 24)
        refresh_sessions_btn.setToolTip("Refresh session list")
        refresh_sessions_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; font-size: 14px; border: none; }"
            "QPushButton:hover { color: #ccc; }"
        )
        refresh_sessions_btn.clicked.connect(self._refresh_session_list)
        session_header.addWidget(refresh_sessions_btn)
        left_layout.addLayout(session_header)
        left_layout.setSpacing(2)

        self.session_list = QListWidget()
        self.session_list.setIconSize(QSize(16, 16))
        self.session_list.itemClicked.connect(self._on_session_clicked)
        self.session_list.itemDoubleClicked.connect(self._on_session_double_clicked)
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._on_session_context_menu)
        left_layout.addWidget(self.session_list)

        splitter.addWidget(left)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)

        # ---- Vertical splitter: draggable handle between the (resizable)
        # chat area on top and the message-input area on the bottom. This is
        # what lets the user shrink the chat to make room for a bigger
        # message box, or expand the chat to read more history. ----
        chat_vs_input_splitter = QSplitter(Qt.Vertical)
        chat_vs_input_splitter.setHandleWidth(6)
        chat_vs_input_splitter.setChildrenCollapsible(False)
        middle_layout.addWidget(chat_vs_input_splitter)

        # ---- TOP pane: the (resizable) chat area ----
        chat_container = QWidget()
        chat_vbox = QVBoxLayout(chat_container)
        chat_vbox.setContentsMargins(0, 0, 0, 0)
        chat_vbox.setSpacing(0)

        # ---- Modular chat panel (replaces the old shared QTextEdit) ----
        self.chat_panel = ChatPanel()
        self.chat_panel.set_render_mode(self.agent_policy_dict.get("chat_render_mode", "markdown"))
        self.chat_panel.set_force_wrap(self.agent_policy_dict.get("force_wrap", True))
        self.chat_panel.set_auto_scroll(self.agent_policy_dict.get("auto_scroll_to_bottom", False))
        self.chat_panel.set_verbosity(self.agent_policy_dict.get("verbosity", "Full trace"))
        self.chat_panel.context_changed.connect(self._on_chat_context_changed)
        self.chat_panel.rewind_requested.connect(self._on_rewind_to)
        self.chat_panel.duplicate_requested.connect(self._on_duplicate_from)
        self.chat_panel.duplicate_message_requested.connect(self._on_duplicate_message)
        self.chat_panel.message_edited.connect(self._on_message_edited)
        chat_vbox.addWidget(self.chat_panel)
        chat_vs_input_splitter.addWidget(chat_container)

        # ---- BOTTOM pane: status + verbosity + attachments + message input ----
        input_container = QWidget()
        input_vbox = QVBoxLayout(input_container)
        input_vbox.setContentsMargins(4, 2, 4, 4)
        input_vbox.setSpacing(4)

        self.activity_label = QLabel("Idle")
        self.activity_label.setStyleSheet("color:#8a8a8a; font-style: italic;")
        input_vbox.addWidget(self.activity_label)

        self.perf_label = QLabel("")
        self.perf_label.setStyleSheet("color:#7a7a7a; font-style: italic; font-size:11px;")
        self.perf_label.setVisible(False)
        input_vbox.addWidget(self.perf_label)

        verbosity_row = QHBoxLayout()
        verbosity_row.addWidget(QLabel("Verbosity"))
        self.verbosity_combo = QComboBox()
        self.verbosity_combo.addItems(["Chat only", "Tool summary", "Full trace"])
        self.verbosity_combo.setCurrentText(self.agent_policy_dict.get("verbosity", "Full trace"))
        self.verbosity_combo.currentTextChanged.connect(self._on_verbosity_changed)
        verbosity_row.addWidget(self.verbosity_combo)
        verbosity_row.addStretch()
        input_vbox.addLayout(verbosity_row)

        self._pending_attachments = []  # list of absolute file paths

        self.attachment_row = QHBoxLayout()
        self.attachment_row.setContentsMargins(0, 0, 16, 0)
        self.attachment_row.setSpacing(6)
        self.attachment_row_widget = QWidget()
        self.attachment_row_widget.setLayout(self.attachment_row)
        self.attachment_scroll = QScrollArea()
        self.attachment_scroll.setWidgetResizable(True)
        self.attachment_scroll.setWidget(self.attachment_row_widget)
        self.attachment_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.attachment_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_scroll.setFixedHeight(36)
        self.attachment_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.attachment_scroll.viewport().setStyleSheet("margin-right: 17px;")
        self.attachment_scroll.setVisible(False)
        input_vbox.addWidget(self.attachment_scroll)

        input_row = QHBoxLayout()
        self.input_edit = ChatInputEdit()
        self.input_edit.setPlaceholderText("Describe what you want the agent to build or fix... (Shift+Enter for newline, drag files here)")
        # No hard maximum height anymore: the input box can now grow when the
        # user drags the vertical splitter handle up (giving it more room),
        # while a sensible minimum keeps it usable.
        self.input_edit.setMinimumHeight(60)
        self.input_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.input_edit.send_requested.connect(self._send_message)
        self.input_edit.files_dropped.connect(self._add_attachments)
        self.input_edit.image_pasted.connect(lambda p: self._add_attachments([p]))
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setFixedWidth(36)
        self.attach_btn.setToolTip("Attach files or images")
        self.attach_btn.clicked.connect(self._browse_attachments)
        input_row.addWidget(self.attach_btn)
        input_row.addWidget(self.input_edit)
        input_vbox.addLayout(input_row)

        button_row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send_message)
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setFixedWidth(56)
        self.retry_btn.setToolTip(
            "Regenerate the agent's last response: remove it and have the agent "
            "answer the same prompt again. (Roleplay-friendly: does not re-run "
            "tools.)"
        )
        self.retry_btn.clicked.connect(self._on_retry_clicked)
        self.force_btn = QPushButton("Respond")
        self.force_btn.setFixedWidth(72)
        self.force_btn.setToolTip(
            "Force the agent to answer the last message even if the input box is "
            "empty — e.g. after a user message the agent hasn't answered yet. "
            "Nothing is removed and no new text is added."
        )
        self.force_btn.clicked.connect(self._on_force_clicked)
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setFixedWidth(78)
        self.continue_btn.setToolTip(
            "Continue the agent's last message from where it ends, appending to "
            "that same message instead of starting a new one.\n\n"
            "To put words into the agent's mouth: right-click its last message -> "
            "Edit, write how it should sound so far, then press Continue."
        )
        self.continue_btn.clicked.connect(self._on_continue_clicked)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_turn)
        self.cancel_btn.setEnabled(False)
        button_row.addWidget(self.send_btn)
        button_row.addStretch()
        button_row.addWidget(self.retry_btn)
        button_row.addWidget(self.continue_btn)
        button_row.addWidget(self.force_btn)
        button_row.addWidget(self.cancel_btn)
        input_vbox.addLayout(button_row)
        chat_vs_input_splitter.addWidget(input_container)

        # Sensible default proportions: chat gets ~75% of the vertical space,
        # the input area the rest. The user can drag the handle to rebalance.
        chat_vs_input_splitter.setSizes([640, 200])
        splitter.addWidget(middle)

        right = QTabWidget()
        self.right_tabs = right

        self.patch_scroll = QScrollArea()
        self.patch_scroll.setWidgetResizable(True)
        self.patch_panel = PatchReviewPanel()
        self.patch_scroll.setWidget(self.patch_panel)
        self.patch_panel.all_decided.connect(self._on_patches_decided)
        self.change_queue_tab_index = right.addTab(self.patch_scroll, "Change Queue")

        self.tools_tab = ToolsTab(
            lambda: self.executor,
            self._on_tool_toggles_changed,
            self._on_subagent_tool_toggles_changed,
        )
        right.addTab(self.tools_tab, "Tools")

        self.context_tab = ContextTab(
            lambda: self.agent,
            self._on_overflow_policy_changed,
            self._on_compact_requested,
            # The tab must hide the subagent meter when there is no worker model
            # to deploy onto. Read from the executor, which is the single source
            # of truth for "is the feature usable" (kept in sync by
            # _wire_subagent_server), with the agent's own check as a fallback.
            lambda: bool(getattr(self.executor, "subagent_enabled", False))
            or (self.agent.subagent_enabled() if self.agent else False),
        )
        self.context_tab.set_policy_value(self.agent_policy_dict.get("context_overflow_policy", "warn_and_ask"))
        right.addTab(self.context_tab, "Context")

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        right.addTab(self.log_view, "Server Log")

        splitter.addWidget(right)
        splitter.setSizes([220, 620, 420])

        self.statusBar().showMessage("Server: stopped")
        self._server_phase_label = ""

    # ---------- Verbosity / tool trace ----------

    def _on_duplicate_from(self, message_id: int):
        """Branch the current chat into a new session, cut at this message.

        The non-destructive sibling of Rewind: same cut point, but nothing is
        removed here — the copy is what gets truncated, and we move into it.
        """
        self._duplicate_session(
            self.session_manager.get_current_id() if self.session_manager else None,
            cut_from_message_id=message_id,
        )

    def _on_duplicate_message(self, message_id: int):
        """Insert an editable copy of one bubble directly below the original.

        A cheap "what if this line went differently" that stays inside the same
        chat — the raw history is deliberately NOT touched, because a second
        identical raw message would double the turn and confuse the pairing in
        core/edit_tracking.py. So the copy is a scratch bubble: the agent's next
        request still sees the original. Use Branch-from-here when the alternate
        wording is the one you actually want to continue from.
        """
        idx = self.chat_panel.message_index(message_id)
        if idx is None:
            return
        original = self.chat_panel.messages[idx]
        if original.streaming:
            return
        copy_msg = original.duplicate()
        self.chat_panel.insert_message(copy_msg, idx + 1)
        self._autosave_current_session()

    def _on_verbosity_changed(self, value: str):
        self.agent_policy_dict["verbosity"] = value
        self.app_settings.save_agent_policy(self.agent_policy_dict)
        # Pure show/hide over already-rendered widgets — cannot alter or
        # lose any streamed content, unlike the old full clear+replay.
        self.chat_panel.set_verbosity(value)

    def _on_chat_context_changed(self):
        """A message's in-context checkbox was toggled by the user. Nothing
        to persist here immediately — context_snapshot() is read live from
        agent.py / session save, so the flag just needs to live on the
        ChatMessage object, which it already does."""
        pass

    def _on_rewind_to(self, message_id: int):
        """Rewind the chat to a specific message, deleting all messages after it."""
        if not self.agent:
            return

        # Find the message in the chat panel to get its position
        msg_index = None
        for i, m in enumerate(self.chat_panel.messages):
            if m.id == message_id:
                msg_index = i
                break
        if msg_index is None:
            return

        # Keep messages up to and including the selected message
        kept_chat_messages = self.chat_panel.messages[:msg_index + 1]
        removed_chat_messages = self.chat_panel.messages[msg_index + 1:]
        # Full panel before the trim, so the rewind can be undone if the raw
        # history turns out not to line up (see below). Shallow on purpose: the
        # ChatMessage objects themselves are never mutated by a rewind.
        all_chat_messages = list(self.chat_panel.messages)

        # Clear live streaming message refs if they were removed
        for m in removed_chat_messages:
            if m is self._live_content_msg:
                self._live_content_msg = None
            if m is self._live_reasoning_msg:
                self._live_reasoning_msg = None
            for attr in ("_live_subagent_content_msg",
                         "_live_subagent_reasoning_msg",
                         "_live_subagent_tool_msg"):
                if getattr(self, attr, None) is m:
                    setattr(self, attr, None)

        # Rebuild the chat panel with only the kept messages
        self.chat_panel.rewind_to(message_id)

        # Trim the agent's raw conversation messages to end EXACTLY at the
        # message the user rewound to. The chat panel and agent.messages are
        # two different lists (the panel has display-only bubbles; the raw
        # history has tool-call wrappers), so the old strategy of counting user
        # turns was unreliable — and it was the source of this bug: rewinding
        # to a *user* message still kept the assistant response that followed
        # it (the code unconditionally included the assistant after the last
        # kept user turn). An edited agent response therefore survived the
        # rewind in the model's context, and "Respond" then continued from that
        # stale text instead of answering the user's message fresh.
        #
        # Instead, resolve the head bubble to its exact raw message using the
        # same structural pairing that targets edits (core.edit_tracking), and
        # trim to and including that index. This distinguishes "rewound to a
        # user message" (trim to that user message, no trailing assistant) from
        # "rewound to an assistant message" (trim to and including that
        # assistant message).
        head_index = len(kept_chat_messages) - 1
        kinds = [m.kind for m in kept_chat_messages]
        texts = [m.text for m in kept_chat_messages]
        head_raw_idx, head_reason = edit_tracking.resolve_bubble(
            self.agent.messages, kinds, texts, head_index)

        if self.agent.messages:
            if head_raw_idx is not None:
                new_agent_messages = self.agent.messages[:head_raw_idx + 1]
            elif edit_tracking.is_unresolvable(head_reason):
                # The head is a display-only bubble (a tool trace, a system
                # notice, ...) with no raw counterpart. Fall back to trimming
                # to the last user message among the kept turns — deliberately
                # WITHOUT the assistant that follows it, which is exactly the
                # message the rewind is meant to remove.
                user_indices = [i for i, m in enumerate(self.agent.messages)
                                if m.get("role") == "user"]
                if user_indices:
                    new_agent_messages = self.agent.messages[:user_indices[-1] + 1]
                else:
                    new_agent_messages = self.agent.messages[:1]
            else:
                # The bubble's position in the raw history could not be
                # established at all (a stale bubble, an approval notice with
                # no raw twin, the panel ahead of the raw history). Any cut we
                # could guess would leave the panel and the raw history
                # disagreeing about what was said — which is the desync that
                # corrupts edits and the tool protocol downstream. So the panel
                # is restored to its pre-rewind state, the raw history is left
                # untouched, and the user is told instead of being handed a
                # silent half-rewind.
                self.chat_panel.rebuild_from(all_chat_messages)
                self.chat_panel.add_message(ChatMessage.create(
                    "system",
                    f"Rewind cancelled — I could not line this bubble up with "
                    f"the model's history ({head_reason}). Nothing was "
                    f"removed.",
                    color="#d19900", in_context=False, min_verbosity=1,
                ))
                return

            self.agent.import_messages(new_agent_messages)

        # Autosave the trimmed state
        self._autosave_current_session()

        # Clear streaming counters since we've changed history
        self._stream_char_count = 0
        self._chunk_count = 0

    def _verbosity_level(self) -> int:
        return VERBOSITY_LABELS.get(self.agent_policy_dict.get("verbosity", "Full trace"), 2)

    def _on_tool_event(self, phase: str, tool_name: str, payload: dict):
        verbosity_name = self.agent_policy_dict.get("verbosity", "Full trace")
        # think_out_loud is always visible in chat regardless of verbosity
        if tool_name == "think_out_loud" and phase == "result":
            thought = payload.get("thought", "") or payload.get("summary", "")
            self.chat_panel.add_message(ChatMessage.create("thought", thought, min_verbosity=0))
            return
        # end_turn summary is always visible in chat regardless of verbosity
        if tool_name == "end_turn" and phase == "result":
            summary = payload.get("summary", "") or ""
            if summary.strip():
                self.chat_panel.add_message(ChatMessage.create("assistant", summary, min_verbosity=0))
                # result.assistant_text will equal this exact summary when the
                # model called end_turn with no preceding streamed content —
                # _on_step_finished must not re-append it as a second bubble.
                self._end_turn_summary_shown = True
            return
        if verbosity_name == "Chat only":
            return
        if phase == "call":
            if verbosity_name == "Full trace":
                display_args = _format_tool_args(payload)
                self.chat_panel.add_message(
                    ChatMessage.create("tool_call", f"→ {tool_name}({display_args})")
                )
        elif phase == "result":
            # Special handling for run_command: show the full command output in chat
            if tool_name == "run_command":
                output = payload.get("output", "")
                command = payload.get("command", "")
                self.chat_panel.add_message(
                    ChatMessage.create("tool_result", f"$ {command}\n\n{output}",
                                        role_label="Command output")
                )
                return
            # When the payload carries a "summary" field, that IS the full
            # tool result text now (agent.py no longer truncates it) — show
            # it directly rather than as a "summary=<text>" key=value pair,
            # so the collapsed one-liner / expanded full text both render
            # cleanly.
            if "summary" in payload:
                other_parts = {k: v for k, v in payload.items() if k != "summary"}
                prefix = f"({_format_tool_args(other_parts)}) " if other_parts else ""
                display_result = f"{prefix}{payload['summary']}"
            else:
                display_result = _format_tool_args(payload)
            self.chat_panel.add_message(
                ChatMessage.create("tool_result", f"✓ {tool_name}: {display_result}")
            )
        elif phase == "error":
            self.chat_panel.add_message(
                ChatMessage.create("tool_error", f"✗ {tool_name} failed: {payload.get('error')}")
            )

    # ---------- Startup server detection ----------

    def _check_existing_server(self):
        base_url = self.server_config.base_url()
        if probe_endpoint(base_url):
            dialog = ServerDetectDialog(base_url, self)
            if dialog.exec():
                if dialog.choice == "attach":
                    self.runtime.adopt_external()
                elif dialog.choice == "restart":
                    self.runtime.stop()
                    QTimer.singleShot(500, self._start_server)

    # ---------- Project handling ----------

    def _open_project(self):
        start_path = self.app_settings.load_last_project()
        if start_path and not os.path.isdir(start_path):
            parent = os.path.dirname(os.path.abspath(start_path))
            if parent and os.path.isdir(parent):
                start_path = parent
            else:
                start_path = ""
        path = QFileDialog.getExistingDirectory(self, "Select project folder", start_path)
        if not path:
            return
        self._load_project(path)

    def _open_history(self):
        if not self.snapshot_store or not self.workspace:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        dialog = HistoryDialog(self.snapshot_store, self.workspace, self)
        dialog.exec()

    def _open_project_directory(self):
        if not self.workspace:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        import subprocess
        import sys
        path = str(self.workspace.root)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _load_project(self, path: str):
        try:
            self.workspace = Workspace(path)
            self.snapshot_store = SnapshotStore(path)
            self.session_manager = SessionManager(path)
            self.draft_store = DraftStore(
                path,
                merge_gap=self.draft_policy_dict.get("merge_gap", 15),
                bulk_threshold=self.draft_policy_dict.get("bulk_threshold", 0.5),
            )
            self.draft_store.load_from_disk()
            self.executor = ToolExecutor(
                self.workspace,
                enabled_tools=dict(self.tool_toggles) if self.tool_toggles else None,
                draft_store=self.draft_store,
                subagent_enabled_tools=(
                    dict(self.subagent_tool_toggles) if self.subagent_tool_toggles else None),
            )
            self._apply_web_policy_to_executor()
            self._apply_web_fetch_policy_to_agent()
            # Re-attach the subagent endpoint here: set_main_config() both
            # inherits the current binary path and adopts a server that is
            # already answering on the configured port.
            self._wire_subagent_server()
            if self.draft_store.list_active():
                patches = self._generate_draft_patches_from_store(self.draft_store)
                if patches:
                    self.patch_panel.add_patches(patches)
                    self._update_change_queue_badge()
        except WorkspaceError as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self.project_label.setText(f"Project: {path}")
        self.app_settings.save_last_project(path)
        self._refresh_file_list()
        context_docs = self.workspace.read_context_docs()
        client = self._build_client()
        policy = self._make_policy()
        self.agent = Agent(client, self.executor, context_docs=context_docs, policy=policy,
                            context_limit=self.server_config.context_size,
                            activity_callback=(lambda state: None),
                            draft_store=self.draft_store,
                            subagent_client_provider=self._build_subagent_client,
                            subagent_policy_dict=self.subagent_policy_dict)
        # deploy_subagent is only offered to the model when a subagent model is
        # actually configured AND enabled (single source of truth for both the
        # schema list and the Tools tab).
        self.executor.subagent_enabled = (
            bool(self.subagent_policy_dict.get("enabled"))
            and SubagentPolicy.from_dict(self.subagent_policy_dict).is_configured()
        )
        # The Context tab shows/hides its subagent meter on this flag, so a
        # settings change has to repaint it (the 500 ms timer only refreshes
        # while a turn is running).
        self.context_tab.refresh()
        self._apply_vision_policy_to_agent()
        self._apply_command_policy_to_agent()

        self.chat_panel.clear()
        # in_context=False: this is an app-log line, not something the model
        # needs re-fed on every turn — visible in chat, invisible to context.
        self.chat_panel.add_message(ChatMessage.create(
            "system", f"Opened project: {path}", in_context=False, min_verbosity=1,
        ))
        if context_docs.strip():
            self.chat_panel.add_message(ChatMessage.create(
                "system", "Loaded context docs (README/AGENT/NOTES) into agent memory.",
                in_context=False, min_verbosity=1,
            ))

        restored = self._load_autosave_chat(path)
        if restored:
            self.agent.import_messages(restored.get("agent_messages", restored) if isinstance(restored, dict) else restored)
            # Same rule as _restore_session: the persisted system message is a
            # snapshot of the prompt active at save time, not the current
            # setting. Re-apply the live prompt so an app restart never
            # resurrects a prompt the user has since changed.
            self.agent.refresh_system_prompt()
            self.chat_panel.add_message(ChatMessage.create(
                "system", "Restored previous conversation from last session.",
                in_context=False, min_verbosity=1,
            ))
            saved_chat_dicts = restored.get("chat_messages") if isinstance(restored, dict) else None
            if saved_chat_dicts:
                for d in saved_chat_dicts:
                    self.chat_panel.add_message(ChatMessage.from_dict(d))
            else:
                # Legacy autosave format with no chat_messages: fall back to a
                # minimal user/assistant-only replay rather than dropping history.
                self._legacy_replay(self.agent.messages)

        self.tools_tab.refresh()
        self.context_tab.refresh()
        # A freshly-opened project is idle — make sure the Retry/Respond
        # buttons reflect that (a new agent exists, no worker running).
        self._update_retry_force_buttons()
        if self.session_manager:
            if not self.session_manager.get_current_id():
                self.session_manager.create_session(first_message="")
            self._refresh_session_list()

    def _legacy_replay(self, messages: list):
        """Best-effort replay for autosave files saved before the modular
        chat panel existed (plain agent messages only, no ChatMessage dicts).
        Only user/assistant text survives — tool calls and reasoning were
        never persisted in that old format, so there's nothing more to show."""
        for m in messages[1:]:
            role = m.get("role")
            content = m.get("content")
            text = content if isinstance(content, str) else ""
            if isinstance(content, list):
                text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            if role == "user" and text:
                self.chat_panel.add_message(ChatMessage.create("user", text))
            elif role == "assistant" and text:
                self.chat_panel.add_message(ChatMessage.create("assistant", text))

    def _autosave_path(self, project_path: str) -> str:
        import os
        import hashlib
        app_dir = self.app_settings.config_dir()
        autosave_dir = os.path.join(app_dir, "autosave_chats")
        os.makedirs(autosave_dir, exist_ok=True)
        key = hashlib.sha1(os.path.abspath(project_path).encode("utf-8")).hexdigest()[:16]
        safe_name = os.path.basename(os.path.normpath(project_path)) or "project"
        return os.path.join(autosave_dir, f"{safe_name}_{key}.json")

    def _save_autosave_chat(self):
        """Persist BOTH the agent's raw messages (for the model) and the
        full ChatMessage list (for faithful UI restore — tool calls,
        reasoning, in_context flags, everything). Previously only the raw
        agent messages were saved, which is why tool calls/reasoning
        vanished after a restart even though they'd survived a Settings
        save during the same session."""
        if not self.agent or not self.workspace:
            return
        import json
        try:
            payload = {
                "agent_messages": self.agent.export_messages(),
                "chat_messages": self.chat_panel.to_persist_dicts(),
            }
            with open(self._autosave_path(str(self.workspace.root)), "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    def _load_autosave_chat(self, project_path: str):
        import json
        import os
        p = self._autosave_path(project_path)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Accept both the new {"agent_messages":..., "chat_messages":...}
            # shape and the old bare-list shape for backward compatibility.
            return data
        except Exception:
            return None

    def _refresh_file_list(self):
        """Keep _refresh_file_list for compatibility — now a no-op since
        the left sidebar shows sessions instead of files."""
        pass

    def _refresh_session_list(self):
        """Populate the left sidebar with all sessions for the current project."""
        self.session_list.clear()
        if not self.session_manager:
            return
        self.session_list.setStyleSheet(
            "QListWidget { "
            "  background: #1e1e1e; "
            "  border: none; "
            "  outline: none; "
            "  font-size: 12px; "
            "  color: #cccccc; "
            "}"
            "QListWidget::item { "
            "  padding: 4px 8px; "
            "  border-radius: 4px; "
            "  margin: 1px 2px; "
            "}"
            "QListWidget::item:selected { "
            "  background: #2a2a3a; "
            "  color: #e6e6e6; "
            "}"
            "QListWidget::item:hover { "
            "  background: #252525; "
            "}"
        )
        sessions = self.session_manager.list_sessions()
        current_id = self.session_manager.get_current_id()
        for s in sessions:
            branch_mark = "⑂ " if getattr(s, "parent_id", None) else "💬 "
            item = QListWidgetItem(f"{branch_mark}{s.title}")
            item.setData(1000, s.id)
            tip = s.preview or ""
            if getattr(s, "parent_id", None):
                parent = self.session_manager._sessions_cache.get(s.parent_id)
                origin = parent.title if parent else "a branched-away chat"
                tip = f"Branched from: {origin}" + (f"\n\n{tip}" if tip else "")
            if tip:
                item.setToolTip(tip)
            if s.id == current_id:
                item.setForeground(QColor("#e6e6e6"))
            else:
                item.setForeground(QColor("#999999"))
            self.session_list.addItem(item)
        for i in range(self.session_list.count()):
            item = self.session_list.item(i)
            if item.data(1000) == current_id:
                self.session_list.setCurrentItem(item)
                break

    def _periodic_context_refresh(self):
        if self.agent is not None and self.worker is not None and self.worker.isRunning():
            self.context_tab.refresh()

    def _generate_draft_patches_from_store(self, draft_store):
        """Generate split-diff patches from a DraftStore for UI display."""
        all_patches = []
        for draft_path in draft_store.list_active():
            try:
                disk_content = self.workspace.read_file(draft_path)
            except WorkspaceError:
                disk_content = ""
            region_patches = draft_store.generate_split_patches(
                draft_path, disk_content,
                merge_gap=self.draft_policy_dict.get("merge_gap", 15),
                bulk_threshold=self.draft_policy_dict.get("bulk_threshold", 0.5),
            )
            all_patches.extend(region_patches)
        return all_patches

    def _make_policy(self) -> AgentPolicy:
        d = self.agent_policy_dict
        return AgentPolicy(
            patch_mode=d.get("patch_mode", "strict_review"),
            batch_review_max=d.get("batch_review_max", 5),
            max_tool_rounds=d.get("max_tool_rounds", 6),
            context_overflow_policy=d.get("context_overflow_policy", "warn_and_ask"),
            stream_to_chat=d.get("stream_to_chat", True),
            auto_expand_percent=d.get("auto_expand_percent", 20),
            reasoning_retention=d.get("reasoning_retention", "keep_last"),
            chat_render_mode=d.get("chat_render_mode", "markdown"),
            auto_scroll_to_bottom=bool(d.get("auto_scroll_to_bottom", False)),
            system_prompt=d.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
            enable_thinking=d.get("enable_thinking", True),
            preserve_thinking=d.get("preserve_thinking", False),
            prune_aggression=d.get("prune_aggression", "off"),
            delete_tool_keep_count=d.get("delete_tool_keep_count", 1),
            merge_gap=self.draft_policy_dict.get("merge_gap", 15),
            bulk_threshold=self.draft_policy_dict.get("bulk_threshold", 0.5),
            time_barriers_enabled=self.time_barriers_policy.get("enabled", False),
            time_barriers=self.time_barriers_policy.get("barriers", []),
        )

    def _apply_command_policy_to_agent(self):
        if self.agent is None:
            return
        cp = self.command_policy_dict
        self.agent.policy.command_run_mode = cp.get("mode", "notify")
        self.agent.policy.command_timeout = cp.get("timeout", 30)
        self.agent.policy.command_allowlist = cp.get("allowlist", [])

    def _apply_web_policy_to_executor(self):
        if self.executor is None:
            return
        self.executor.web_policy = self.web_policy_dict.get("mode", "off")
        self.executor.web_allowlist = self.web_policy_dict.get("allowlist", [])
        self.executor.web_blocklist = self.web_policy_dict.get("blocklist", [])
        if "tor_socks_host" in self.web_policy_dict or "tor_socks_port" in self.web_policy_dict:
            try:
                self.executor.set_tor_proxy(
                    self.web_policy_dict.get("tor_socks_host", "127.0.0.1"),
                    int(self.web_policy_dict.get("tor_socks_port", 9050)),
                )
            except (ValueError, TypeError):
                pass

    def _apply_web_fetch_policy_to_agent(self):
        if self.agent is None:
            return
        self.agent._web_fetch_policy = self.web_fetch_policy_dict

    # ---------- Vision ----------

    def _vision_available(self) -> bool:
        vp = self.vision_policy_dict
        if vp.get("mode") == "disabled":
            return False
        if not self.server_config.managed:
            return True
        return bool((vp.get("mmproj_path") or self.server_config.mmproj_path or "").strip())

    def _build_captioner(self):
        vp = self.vision_policy_dict
        if not vp.get("caption_fallback_enabled", False) or vp.get("mode") == "mmproj_only":
            return None
        try:
            from core.caption_fallback import ImageCaptioner, deps_status
            ok, msg = deps_status()
            if not ok:
                self._append_log(f"Image-to-text fallback disabled: {msg}")
                return None
            return ImageCaptioner(vp.get("caption_model", ""), vp.get("caption_device", "auto"))
        except Exception as e:
            self._append_log(f"Image-to-text fallback unavailable: {e}")
            return None

    def _apply_vision_policy_to_agent(self):
        if self.agent is None:
            return
        self.agent.vision_available = self._vision_available()
        self.agent.captioner = self._build_captioner()
        if not self.agent.vision_available and self.agent.captioner is not None:
            self._append_log(
                f"No mmproj projector configured — view_image will fall back to text descriptions "
                f"from {self.agent.captioner.label}."
            )


    def _wire_subagent_server(self):
        """(Re)build the subagent policy onto the runtime and adopt an endpoint
        that is already listening. Safe to call repeatedly and with no project
        open.

        set_main_config() must run here, on the GUI thread: it is what adopts a
        live endpoint, and building a LlamaServerRuntime from a worker thread
        (no Qt event loop) is a hard SIGSEGV. The runtime keeps the main config
        only to inherit the binary path and the KV/flash-attn/thread knobs; the
        subagent's own port, context, and GPU-vs-RAM placement always come from
        its own policy."""
        policy = SubagentPolicy.from_dict(self.subagent_policy_dict)
        self.subagent_server.policy = policy
        self.subagent_server.set_main_config(self.server_config)
        # Keep the deploy_subagent tool's visibility in sync with whether there
        # is a model to deploy onto, and hand the Agent the current subagent
        # settings (max_rounds, streaming, thinking) so the next deployment uses
        # them. Both are cheap no-ops when the respective object is absent.
        usable = bool(self.subagent_policy_dict.get("enabled")) and policy.is_configured()
        if self.executor is not None:
            self.executor.subagent_enabled = usable
        if self.agent is not None:
            self.agent.subagent_policy_dict = dict(self.subagent_policy_dict)
        # The Context tab's subagent meter depends on both the executor flag and
        # the agent's policy dict — repaint so a settings change shows up without
        # waiting for the next turn.
        if getattr(self, "context_tab", None) is not None:
            self.context_tab.refresh()
        if policy.is_configured():
            self._append_log(
                f"[subagent] model server configured on port {policy.port} "
                f"(ctx={policy.context_size}, placement={policy.device})"
            )

    def _apply_subagent_policy(self):
        """Called after a Settings save: re-wire onto the new policy, and drop a
        server that no longer matches it.

        The check is a full command-line signature comparison
        (SubagentPolicy.signature), not a port comparison. Port was the only
        thing that could previously go stale while the feature stayed enabled,
        but that is no longer true now that the subagent has its own KV cache
        type: a quantization change is another startup flag, so leaving the old
        child up would keep serving with the old KV buffer while the Settings
        dialog showed the new one. Any flag-affecting change — port, model,
        context, placement, extra args, KV types — now stops the child so the
        next start picks it up.

        An endpoint merely *adopted* (spawned_signature() is None) is never
        killed — it is not our child, and we have no idea what it was started
        with, so restarting it over a settings tweak would be worse than
        reporting that it needs restarting by hand."""
        policy = SubagentPolicy.from_dict(self.subagent_policy_dict)
        was_up = self.subagent_server.runtime is not None
        old_port = self.subagent_server.policy.port
        # Captured before _wire_subagent_server() replaces the runtime's policy:
        # this is what the running child was actually started with, or None if
        # this app never spawned it.
        old_signature = self.subagent_server.spawned_signature()
        # Did a child we spawned exist before the re-wire? Checked separately
        # from was_up, because an adopted endpoint (external llama-server) is
        # "up" without ever having a signature we could compare.
        was_spawned = old_signature is not None
        self._wire_subagent_server()

        if not policy.is_configured():
            if was_up:
                try:
                    self.subagent_server.stop()
                    self._append_log("[subagent] disabled — subagent server stopped")
                except Exception as e:
                    self._append_log(f"[subagent] could not stop subagent server: {e}")
            return

        if not was_spawned:
            # Nothing we spawned: either no server is running (the next
            # deployment starts it with the new settings — nothing to do) or the
            # endpoint was adopted from outside this app, in which case a
            # settings change can only be reported, not applied.
            if was_up:
                self._append_log(
                    "[subagent] settings changed, but the server on port "
                    f"{old_port} was adopted rather than started by this app — "
                    "restart it yourself to apply the new settings"
                )
            return

        if was_up and old_signature != policy.signature():
            self._append_log(
                f"[subagent] startup settings changed (port {old_port} -> "
                f"{policy.port}, KV k={policy.kv_cache_type_k or '<inherit>'} "
                f"v={policy.kv_cache_type_v or '<inherit>'}); stopping the old "
                "server so the next start uses them"
            )
            try:
                self.subagent_server.stop()
            except Exception as e:
                self._append_log(f"[subagent] could not stop stale subagent server: {e}")

    # ---------- Server management ----------

    def _open_settings(self):
        dialog = SettingsDialog(self.server_config, self.agent_policy_dict, self.web_policy_dict,
                                self.vision_policy_dict, self.draft_policy_dict,
                                self.time_barriers_policy, self.subagent_policy_dict, self)
        dialog.command_whitelist.set_policy(self.command_policy_dict)
        if dialog.exec():
            # Captured before self.agent_policy_dict is replaced below: this is
            # the system prompt actually baked into agent.messages[0] right now,
            # which is what we compare against to decide whether the running
            # conversation has to be re-primed.
            old_system_prompt = self.agent_policy_dict.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
            self.server_config = dialog.get_config()
            self.agent_policy_dict = dialog.get_policy()
            self.draft_policy_dict = dialog.get_draft_policy()
            self.web_policy_dict = dialog.get_web_policy()
            self.web_fetch_policy_dict = dialog.get_web_fetch_policy()
            self.vision_policy_dict = dialog.get_vision_policy()
            self.subagent_policy_dict = dialog.get_subagent_policy()
            self.command_policy_dict = dialog.get_command_policy()
            self.time_barriers_policy = dialog.get_time_barriers_policy()
            self.app_settings.save_server_config(self.server_config)
            self.app_settings.save_agent_policy(self.agent_policy_dict)
            self.app_settings.save_draft_policy(self.draft_policy_dict)
            self.app_settings.save_web_policy(self.web_policy_dict)
            self.app_settings.save_web_fetch_policy(self.web_fetch_policy_dict)
            self.app_settings.save_vision_policy(self.vision_policy_dict)
            self.app_settings.save_subagent_policy(self.subagent_policy_dict)
            self.app_settings.save_command_policy(self.command_policy_dict)
            self.app_settings.save_time_barriers(
                self.time_barriers_policy.get("enabled", False),
                self.time_barriers_policy.get("barriers", []),
            )
            self.app_settings.save_prune_aggression(self.agent_policy_dict.get("prune_aggression", "off"))
            self.app_settings.save_delete_tool_keep_count(self.agent_policy_dict.get("delete_tool_keep_count", 1))
            self._apply_web_policy_to_executor()
            self._apply_web_fetch_policy_to_agent()
            self._apply_subagent_policy()
            if self.agent:
                self.agent.client = self._build_client()
                self.agent.policy = self._make_policy()
                self.agent.context_limit = self.server_config.context_size
                self._apply_vision_policy_to_agent()
                self._apply_command_policy_to_agent()
                self.agent.policy.time_barriers_enabled = self.time_barriers_policy.get("enabled", False)
                self.agent.policy.time_barriers = self.time_barriers_policy.get("barriers", [])
                self.agent.policy.prune_aggression = self.agent_policy_dict.get("prune_aggression", "off")
                self.agent.policy.delete_tool_keep_count = self.agent_policy_dict.get("delete_tool_keep_count", 1)
                # Re-apply the system prompt to the LIVE conversation. Setting
                # agent.policy alone was never enough: the prompt is only ever
                # read when the leading system message is built (Agent.reset()),
                # so a save left messages[0] — and therefore every request, the
                # context meter, and both persistence layers — pinned to the
                # prompt that was active when the session started. Replacing it
                # now means the next request uses the new prompt without
                # discarding the conversation.
                new_system_prompt = self.agent_policy_dict.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
                if new_system_prompt != old_system_prompt and self.agent.refresh_system_prompt():
                    # Persisting is what actually un-sticks the old prompt: both
                    # the autosave and the per-session file store a copy of
                    # messages[0], so without this rewrite a later resume would
                    # still hand back the stale text.
                    self._save_autosave_chat()
                    self._autosave_current_session()
                    self.context_tab.refresh()
                    self.chat_panel.add_message(ChatMessage.create(
                        "system",
                        "System prompt updated — applied to this conversation immediately. "
                        "The new prompt is used for the next request; no new session is needed.",
                        color="#d19900", in_context=False, min_verbosity=1,
                    ))
            if self.draft_store:
                self.draft_store.merge_gap = self.draft_policy_dict.get("merge_gap", 15)
                self.draft_store.bulk_threshold = self.draft_policy_dict.get("bulk_threshold", 0.5)
            self.context_tab.set_policy_value(self.agent_policy_dict.get("context_overflow_policy", "warn_and_ask"))
            self.verbosity_combo.setCurrentText(self.agent_policy_dict.get("verbosity", "Full trace"))
            # Pure visibility + render-mode refresh over EXISTING widgets.
            # This can no longer drop or duplicate a single message — there
            # is nothing to "replay", every widget is already there.
            self.chat_panel.set_verbosity(self.agent_policy_dict.get("verbosity", "Full trace"))
            self.chat_panel.set_render_mode(self.agent_policy_dict.get("chat_render_mode", "markdown"))
            self.chat_panel.set_force_wrap(self.agent_policy_dict.get("force_wrap", True))
            self.chat_panel.set_auto_scroll(self.agent_policy_dict.get("auto_scroll_to_bottom", False))
            if self.tools_tab:
                self.tools_tab.refresh()

    def _build_client(self) -> LLMClient:
        return LLMClient(base_url=self.server_config.base_url())

    def _build_subagent_client(self) -> LLMClient:
        """Return a live LLMClient for the subagent model server, or raise
        RuntimeError with a user-actionable message.

        Injected into the Agent as its `subagent_client_provider` and called
        lazily on each deploy_subagent. Runs on the AgentWorker thread during a
        turn, which is exactly the thread SubagentServerRuntime.start() is built
        to run on: when nothing is listening yet it self-heals by spawning the
        server detached and waiting for it, so the very first deployment brings
        the model up instead of failing. It deliberately raises rather than
        returning None so _deploy_subagent turns "no model here" into a readable
        tool result the main agent can relay — the subagent client must never be
        a silent None the runner then trips over.
        """
        if not self.subagent_policy_dict.get("enabled"):
            raise RuntimeError(
                "the subagent model is disabled. Tick 'Enable subagent model' "
                "under Settings → Backend and start the server."
            )
        policy = SubagentPolicy.from_dict(self.subagent_policy_dict)
        if not policy.is_configured():
            raise RuntimeError(
                "no subagent model is configured. Choose a small text GGUF under "
                "Settings → Backend → Subagent model."
            )
        # Keep the runtime pointed at the live policy (model, port, device may
        # have changed since it was last wired) before we ask it to come up.
        self.subagent_server.policy = policy
        try:
            ready = self.subagent_server.start(on_log=self._append_log)
        except Exception as e:
            raise RuntimeError(f"the subagent server could not be started: {e}")
        if not ready:
            detail = self.subagent_server.last_error() or "it did not become ready"
            raise RuntimeError(f"the subagent model server is not available: {detail}")
        return LLMClient(base_url=policy.base_url())

    def _start_server(self):
        self.runtime.start(self.server_config)
        # The subagent model loads concurrently with the main model rather than
        # on first use: it is small, so paying its (short) load cost here is what
        # keeps a deployment from stalling. Kept on its own QThread so a slow
        # disk never freezes the UI, and stored on self so the worker is not
        # garbage-collected mid-run (Qt kills a QThread destroyed while running).
        if (self.subagent_policy_dict.get("enabled")
                and (self.subagent_policy_dict.get("model_path") or "").strip()):
            from core.subagent_server import SubagentStartWorker

            self._subagent_start_worker = SubagentStartWorker(self.subagent_server, self._append_log)
            self._subagent_start_worker.finished_start.connect(self._on_subagent_server_started)
            self._subagent_start_worker.start()

    def _on_subagent_server_started(self, ok: bool, message: str):
        self._append_log("[subagent] server ready" if ok else f"[subagent] {message}")

    def _stop_server(self):
        self.runtime.stop()
        # An in-flight subagent startup blocks in SubagentServerRuntime.start()
        # (detached spawn + readiness wait), so wait for it to register its child
        # PID before stopping, or the child outlives "Stop Server" and keeps the
        # port — and later probes on it get answered by the wrong model.
        sa_worker = getattr(self, "_subagent_start_worker", None)
        if sa_worker is not None and sa_worker.isRunning():
            sa_worker.wait(10000)
        try:
            self.subagent_server.stop()
            self._append_log("[subagent] subagent server stopped")
        except Exception as e:
            self._append_log(f"[subagent] could not stop subagent server: {e}")

    def _append_log(self, line: str):
        self.log_view.append(line)

    def _on_perf_metrics(self, metrics: dict):
        token_s = metrics.get("token_s")
        if token_s is not None:
            self._append_log(f"[Performance] {token_s:.1f} token/s (from server log)")

    def _on_server_state(self, state: str):
        self._update_status_bar(state, self._server_phase_label)

    def _on_server_phase(self, phase: str):
        self._server_phase_label = phase
        self._update_status_bar(self.runtime.state(), phase)

    def _update_status_bar(self, state: str, phase: str):
        if phase and phase not in ("Ready", "Stopped"):
            self.statusBar().showMessage(f"Server: {state} — {phase}")
        elif phase == "Ready" and state == "running":
            self.statusBar().showMessage("Server: running")
        else:
            self.statusBar().showMessage(f"Server: {state}")

    # ---------- Policy change callbacks ----------

    def _on_tool_toggles_changed(self, toggles: dict):
        self.tool_toggles = toggles
        self.app_settings.save_tool_toggles(toggles)

    def _on_subagent_tool_toggles_changed(self, toggles: dict):
        """Persisted under its own key and stored on its own dict, so toggling a
        subagent tool never touches the main agent's tool_toggles."""
        self.subagent_tool_toggles = toggles
        self.app_settings.save_subagent_tool_toggles(toggles)

    def _on_overflow_policy_changed(self, value: str):
        self.agent_policy_dict["context_overflow_policy"] = value
        self.app_settings.save_agent_policy(self.agent_policy_dict)
        if self.agent:
            self.agent.policy.context_overflow_policy = value

    def _on_reasoning_policy_changed(self, value: str):
        self.agent_policy_dict["reasoning_retention"] = value
        self.app_settings.save_agent_policy(self.agent_policy_dict)
        if self.agent:
            self.agent.policy.reasoning_retention = value

    def _on_compact_requested(self, strategy: str, keep_count: int = None):
        if not self.agent:
            return
        if keep_count is not None:
            self.agent.policy.delete_tool_keep_count = keep_count
        self._compaction_worker = CompactionWorker(self.agent, strategy=strategy)
        self._compaction_worker.finished_compaction.connect(self._on_compaction_finished)
        self._compaction_worker.failed.connect(self._on_compaction_failed)
        self._compaction_worker.start()

    def _on_compaction_finished(self, summary: str):
        self.context_tab.set_compaction_result(summary)
        self.chat_panel.add_message(ChatMessage.create(
            "system", f"History compacted manually.\n{summary}", color="#d19900",
            in_context=False, min_verbosity=1,
        ))
        self._save_autosave_chat()

    def _on_compaction_failed(self, error: str):
        self.context_tab.compact_btn.setEnabled(True)
        self.chat_panel.add_message(ChatMessage.create("tool_error", f"Compaction failed: {error}"))

    # ---------- Activity indicator ----------

    def _on_activity_changed(self, state: str):
        if state.startswith("tool_call:"):
            tool_name = state.split(":", 1)[1]
            text, color = f"Using tool: {tool_name}...", "#d19900"
            self._in_reading_state = False
        elif state.startswith("waiting_for_model:"):
            self._stream_char_count = 0
            self._chunk_count = 0
            count_str = state.split(":", 1)[1] if ":" in state else ""
            if count_str:
                text, color = f"Thinking... ({count_str} tokens)", "#5591c7"
            else:
                text, color = ACTIVITY_LABELS.get(state, (state, "#8a8a8a"))
            # A token count means prompt processing has finished and decoding
            # has started — drop any stale prefill progress from a prior round.
            self._prompt_progress = None
            self._in_reading_state = False
        elif state == "waiting_for_model":
            # Starting a fresh prompt-processing phase: reset any progress
            # left over from the previous model call so the label starts
            # clean rather than jumping straight to an old percentage.
            self._prompt_progress = None
            self._initial_prompt_progress = None
            self._in_reading_state = True
            text, color = ACTIVITY_LABELS.get(state, (state, "#8a8a8a"))
        else:
            text, color = ACTIVITY_LABELS.get(state, (state, "#8a8a8a"))
            self._in_reading_state = False
        self._set_activity_label(text, color)

    def _set_activity_label(self, text: str, color: str):
        """Single place that writes the activity label — lets the
        prompt-progress handler and the streaming-delta handler both refresh
        it without duplicating the setText/setStyleSheet pair."""
        self.activity_label.setText(text)
        self.activity_label.setStyleSheet(f"color:{color}; font-style: italic;")

    def _on_prompt_progress(self, n_tokens: int, progress: float):
        """Live llama-server prompt-processing progress (from the Server Log
        print_timing lines). While the activity label is in the "Reading..."
        (waiting_for_model) state, fold the percentage into the label so the
        user sees e.g. "Reading... (73%)" updating in real time."""
        if progress is None or progress < 0:
            return
        
        if not self._in_reading_state:
            self._prompt_progress = progress
            return

        if self._initial_prompt_progress is None:
            self._initial_prompt_progress = progress
            
        self._prompt_progress = progress
        
        denom = 1.0 - self._initial_prompt_progress
        if denom > 0:
            rel_progress = (progress - self._initial_prompt_progress) / denom
            rel_progress = max(0.0, min(1.0, rel_progress))
            pct = int(round(rel_progress * 100))
        else:
            pct = 100 if progress >= 1.0 else 0
            
        self._set_activity_label(f"Reading... ({pct}%)", "#5591c7")

    # ---------- Session management ----------

    def _new_session(self):
        if not self.agent or not self.workspace:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        self._autosave_current_session()
        context_docs = self.workspace.read_context_docs()
        self.agent.reset(context_docs=context_docs)
        self.agent.total_generated_tokens = 0
        self.chat_panel.clear()
        self._save_autosave_chat()
        if self.session_manager:
            self.session_manager.create_session(first_message="")
            self._refresh_session_list()
        self.chat_panel.add_message(ChatMessage.create(
            "system", "New session started.", in_context=False, min_verbosity=1,
        ))
        self.context_tab.refresh()

    def _extract_first_user_message(self) -> str:
        for m in self.agent.messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                return content.strip()
        return ""

    def _autosave_current_session(self):
        """Save the current session's agent messages AND full ChatMessage
        list to disk via the session manager — same fix as _save_autosave_chat,
        applied to the per-session store so switching sessions doesn't lose
        tool calls/reasoning either."""
        if not self.agent or not self.session_manager:
            return
        self.session_manager.set_messages(
            self.agent.export_messages(),
            chat_history=self.chat_panel.to_persist_dicts(),
        )
        self.session_manager._update_preview_from_messages(
            self.session_manager._sessions_cache.get(self.session_manager.get_current_id())
            if self.session_manager.get_current_id() else None
        )

    def _on_session_clicked(self, item):
        session_id = item.data(1000)
        if not session_id or not self.session_manager:
            return
        self._restore_session(session_id)

    def _on_session_double_clicked(self, item):
        self._on_session_clicked(item)

    def _on_session_context_menu(self, pos):
        item = self.session_list.itemAt(pos)
        if not item:
            return
        session_id = item.data(1000)
        if not session_id:
            return
        menu = QMenu(self)
        duplicate_action = menu.addAction("⧉  Duplicate session")
        duplicate_action.setToolTip(
            "Copy this conversation into a new session and continue there. The "
            "original is left untouched, so you can take the same chat down two "
            "different paths."
        )
        duplicate_action.triggered.connect(lambda: self._duplicate_session(session_id))
        rename_action = menu.addAction("✎  Rename session")
        rename_action.triggered.connect(lambda: self._rename_session(session_id))
        menu.addSeparator()
        delete_action = menu.addAction("🗑  Delete session")
        delete_action.triggered.connect(lambda: self._delete_session(session_id))
        menu.exec(self.session_list.mapToGlobal(pos))

    def _rename_session(self, session_id: str):
        """Rename a session in the sidebar.

        Renaming is what makes a branch worth having: the duplicate's title is
        derived from its first user message (see _update_preview_from_messages),
        so without a rename two timelines that share an opening look identical
        in the list. The new name is written straight back to the store and the
        preview is kept in step with it, so the tooltip does not contradict the
        label.
        """
        if not self.session_manager:
            return
        session = self.session_manager.get_session(session_id)
        if session is None:
            return
        new_title, ok = QInputDialog.getText(
            self, "Rename session", "Session name:", text=session.title or "",
        )
        if not ok:
            return
        new_title = new_title.strip()
        if not new_title or new_title == session.title:
            return
        session.title = new_title
        session.preview = new_title[:120]
        self.session_manager._save_session_file(session)
        self._refresh_session_list()

    def _duplicate_session(self, session_id: str, cut_from_message_id=None):
        """Duplicate a session (optionally truncated) and switch to the copy.

        The whole point is a non-destructive fork: the source conversation stays
        on disk exactly as it was, and the branch becomes the active chat. When
        ``cut_from_message_id`` is given, the branch is truncated at that bubble
        of the CURRENT chat panel first — that is the "branch from here" entry
        point from a message's context menu.
        """
        if not self.session_manager or not self.agent or not self.workspace:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(
                self, "Agent is working",
                "Wait for the current turn to finish (or cancel it) before "
                "duplicating a session.",
            )
            return

        # Persist what is on screen right now BEFORE the copy is taken,
        # otherwise the duplicate would be made from the last autosave and
        # silently lose everything typed since. This also means duplicating the
        # session you are already in is always safe.
        self._autosave_current_session()

        source = self.session_manager.get_session(session_id)
        if source is None:
            QMessageBox.warning(self, "Duplicate session",
                                "That session could not be loaded.")
            return

        # A message-level branch has to cut the raw history and the rendered
        # history separately: the two lists differ in length (display-only
        # bubbles), so one index cannot serve both.
        keep_messages = None
        keep_chat_history = None
        if cut_from_message_id is not None:
            bubble_index = self.chat_panel.message_index(cut_from_message_id)
            if bubble_index is None:
                # The bubble is gone from the panel — it belongs to a different
                # session, or was trimmed since the menu was opened. Refuse
                # rather than hand back a branch cut at a guessed position.
                QMessageBox.information(
                    self, "Branch from here",
                    "That message is no longer in the current chat, so there is "
                    "nothing to branch from.",
                )
                return
            kinds = [m.kind for m in self.chat_panel.messages]
            texts = [m.text for m in self.chat_panel.messages]
            cut, reason = edit_tracking.raw_cut_for_bubble(
                self.agent.messages, kinds, texts, bubble_index)
            if cut is None:
                QMessageBox.warning(
                    self, "Branch from here",
                    f"I could not line this message up with the model's history "
                    f"({reason}), so no branch was created.",
                )
                return
            keep_messages = cut
            keep_chat_history = edit_tracking.chat_cut_for_raw(
                self.agent.messages, cut, kinds, texts)

        branch = self.session_manager.fork_session(
            session_id,
            keep_messages=keep_messages,
            keep_chat_history=keep_chat_history,
            title=self.session_manager.next_branch_title(source.title),
            make_current=True,
        )
        if branch is None:
            QMessageBox.warning(self, "Duplicate session",
                                "Creating the copy failed.")
            return

        # Load the branch into the agent and the panel. _restore_session
        # re-applies the live system prompt and rebuilds the trace faithfully,
        # so the branch starts from the same footing a normal session switch
        # would. It returns False for an empty branch — a fork taken above the
        # first message — which needs an explicit fresh start instead of being
        # left showing the conversation it was branched away from.
        self._repair_fork_tail(branch)
        if not self._restore_session(branch.id):
            self.agent.reset(context_docs=self.workspace.read_context_docs())
            self.chat_panel.clear()
        self._refresh_session_list()

        self.chat_panel.add_message(ChatMessage.create(
            "system",
            (f"Branched this chat into \"{branch.title}\". The original "
             f"\"{source.title}\" is unchanged and still in the list.")
            if cut_from_message_id is None else
            (f"Branched this chat into \"{branch.title}\" up to the message you "
             f"picked. The original \"{source.title}\" is unchanged and still in "
             f"the list."),
            in_context=False, min_verbosity=1,
        ))
        self._save_autosave_chat()
        self._autosave_current_session()

    def _repair_fork_tail(self, session) -> None:
        """Trim a forked raw history back to a legal prompt boundary.

        A conversation that ended mid-turn does not cut cleanly: the raw history
        can end on a ``tool`` message whose ``tool_calls`` wrapper was also cut,
        or on an assistant message that announced a tool call the branch will
        never answer. OpenAI-compatible servers reject those outright, so a
        branch taken from a message in the middle of a tool run would be a chat
        you could not send a single message in.

        Rather than silently discarding the user's cut, drop trailing messages
        until the tail is either a ``user`` message (the agent answers again) or
        a clean assistant answer with no dangling call.
        """
        msgs = session.messages
        if not msgs:
            return

        def _dangling_tool_results(body) -> bool:
            """Any tool message whose call_id has no assistant tool_calls twin."""
            call_ids = set()
            for m in body:
                if m.get("role") == "assistant":
                    for tc in (m.get("tool_calls") or []):
                        if isinstance(tc, dict) and tc.get("id"):
                            call_ids.add(tc["id"])
            for m in body:
                if m.get("role") == "tool" and m.get("tool_call_id") not in call_ids:
                    return True
            return False

        while len(msgs) > 1:
            tail = msgs[-1]
            role = tail.get("role")
            if role == "user":
                break
            if role == "assistant" and not tail.get("tool_calls"):
                break
            # A tool result is only legal as part of a complete call/result
            # pair, and even then it leaves the model mid-turn — cut it.
            msgs.pop()

        # Re-check the whole body for orphans: popping the tail can strand a
        # tool result whose wrapper is still there but whose sibling call was
        # already dropped by an earlier iteration.
        while len(msgs) > 1 and _dangling_tool_results(msgs[1:]):
            msgs.pop()

    def _delete_session(self, session_id: str):
        if not self.session_manager:
            return
        session = self.session_manager._sessions_cache.get(session_id)
        if not session:
            return

        current_id = self.session_manager.get_current_id()
        is_current = session_id == current_id

        if is_current:
            reply = QMessageBox.question(
                self, "Delete session?",
                "Delete the current session? Its messages will be lost.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        else:
            reply = QMessageBox.question(
                self, "Delete session?",
                f"Delete session '{session.title[:60]}...'?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        was_current = self.session_manager.delete_session(session_id)

        if was_current:
            remaining = self.session_manager.list_sessions()
            if remaining:
                self.session_manager.set_current_session(remaining[0].id)
                self._restore_session(remaining[0].id)
            else:
                self.session_manager.create_session(first_message="")
                self.agent.reset(context_docs=self.workspace.read_context_docs() if self.workspace else "")
                self.chat_panel.clear()
                self.chat_panel.add_message(ChatMessage.create(
                    "system", "Session deleted. New session created.",
                    in_context=False, min_verbosity=1,
                ))

        self._refresh_session_list()

    def _restore_session(self, session_id: str) -> bool:
        """Load a session's messages and chat history into the agent and
        chat panel. rebuild_from() replaces the old per-entry _render_entry
        loop wholesale — every ChatMessage (including in_context/verbosity
        flags) round-trips faithfully now instead of just role_label/text/
        color/kind.

        Returns False when the session has no messages at all, so the caller
        can do something deliberate instead of being left with whatever the
        previous session was still showing.
        """
        if not self.session_manager or not self.agent:
            return False
        messages = self.session_manager.get_session_messages(session_id)
        if not messages:
            return False
        self._autosave_current_session()
        self.session_manager.set_current_session(session_id)
        self.agent.import_messages(messages)
        # A stored session's system message is a snapshot of whatever prompt
        # happened to be active when it was last saved — which is why resuming
        # an old conversation used to silently drag the old system prompt back
        # in. The live Settings value is authoritative, so always re-apply the
        # current prompt to the resumed conversation. This also backfills the
        # leading system message for sessions saved before it was stored.
        if self.agent.refresh_system_prompt():
            self._save_autosave_chat()

        saved_chat = self.session_manager.get_session_chat_history(session_id)
        if saved_chat:
            try:
                restored_msgs = [ChatMessage.from_dict(d) if "kind" in d else self._legacy_dict_to_message(d) for d in saved_chat]
            except Exception:
                restored_msgs = [self._legacy_dict_to_message(d) for d in saved_chat]
            self.chat_panel.rebuild_from(restored_msgs)
        else:
            self.chat_panel.clear()
            self._legacy_replay(messages)

        self._refresh_session_list()
        self.context_tab.refresh()
        return True

    @staticmethod
    def _legacy_dict_to_message(d: dict) -> ChatMessage:
        """Convert an old-format chat-history entry
        ({"role_label","text","color","kind"}) into a ChatMessage, for
        sessions saved before the modular chat panel existed."""
        return ChatMessage.create(
            kind=d.get("kind", "system"),
            text=d.get("text", ""),
            role_label=d.get("role_label"),
            color=d.get("color"),
        )

    # ---------- Attachments ----------
    def _browse_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        if paths:
            self._add_attachments(paths)

    def _add_attachments(self, paths: list):
        for p in paths:
            if p not in self._pending_attachments:
                self._pending_attachments.append(p)
        self._refresh_attachment_row()

    def _remove_attachment(self, path: str):
        self._pending_attachments = [p for p in self._pending_attachments if p != path]
        self._refresh_attachment_row()

    def _refresh_attachment_row(self):
        while self.attachment_row.count():
            item = self.attachment_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for path in self._pending_attachments:
            chip = AttachmentChip(path)
            chip.removed.connect(self._remove_attachment)
            self.attachment_row.addWidget(chip)
        self.attachment_row.addStretch()
        visible = bool(self._pending_attachments)
        self.attachment_scroll.setVisible(visible)

    # ---------- Change Queue badge ----------

    def _update_change_queue_badge(self):
        pending_count = sum(1 for c in self.patch_panel.cards if c.patch.status == "pending")
        idx = self.change_queue_tab_index
        if pending_count > 0:
            self.right_tabs.setTabText(idx, f"Change Queue ({pending_count})")
            self.right_tabs.tabBar().setTabTextColor(idx, QColor("#d19900"))
            if self.right_tabs.currentIndex() != idx:
                self.right_tabs.setCurrentIndex(idx)
        else:
            self.right_tabs.setTabText(idx, "Change Queue")
            self.right_tabs.tabBar().setTabTextColor(idx, QColor())

    # ---------- Agent interaction ----------

    def _send_message(self):
        text = self.input_edit.toPlainText().strip()
        if not text and not self._pending_attachments:
            return
        if not self.agent:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            return

        if self.runtime.state() != "running":
            QMessageBox.information(self, "Server not running", "The llama-server is not running. Start it first via the Start Server button.")
            return

        attachments = list(self._pending_attachments)
        display_text = text or "(no message)"
        if attachments:
            import os as _os
            names = ", ".join(_os.path.basename(p) for p in attachments)
            display_text += f"\n📎 Attached: {names}"
        self.chat_panel.add_message(ChatMessage.create("user", display_text))

        self.input_edit.clear()
        self._pending_attachments = []
        self._refresh_attachment_row()
        self._pending_prompt_summary = text or "attachment"
        self._stream_char_count = 0
        self._chunk_count = 0
        self._perf_start_time = None
        self._perf_content_chunks = 0
        self._live_content_msg = None
        self._live_reasoning_msg = None
        self._had_live_reasoning_this_round = False
        self._had_live_content_this_round = False
        self._end_turn_summary_shown = False
        self._streaming_to_chat = self.agent_policy_dict.get("stream_to_chat", True)

        try:
            self.agent.add_user_message(text, attachments=attachments)
        except TypeError:
            self.agent.add_user_message(text)
        self._autosave_current_session()

        self._lock_buttons_for_turn()
        QTimer.singleShot(30, self._launch_worker)

    def _launch_worker(self):
        use_streaming = self.agent_policy_dict.get("use_streaming", True)
        self.worker = AgentWorker(self.agent, use_streaming=use_streaming)
        self.worker.delta_received.connect(self._on_delta)
        self.worker.activity_changed.connect(self._on_activity_changed)
        self.worker.tool_event.connect(self._on_tool_event)
        self.worker.finished_step.connect(self._on_step_finished)
        self.worker.web_fetch_truncated.connect(self._on_web_fetch_truncated)
        self.worker.failed.connect(self._on_worker_failed)
        self.worker.start()
        self.cancel_btn.setEnabled(True)

    def _cancel_turn(self):
        if self.worker is not None:
            self.worker.cancel()
            self._in_reading_state = False
            self.activity_label.setText("Cancelling...")

    def _prompt_ask_user(self, question: str, choices: list, allow_custom: bool,
                         multi_select: bool = False):
        """Show the ask_user dialog and return the answer as a string.

        Returns None when the user explicitly dismissed the dialog (Cancel,
        window close, or a construction error) rather than answering it — the
        caller must not present that as if the user had said something.
        """
        try:
            dialog = _AskUserDialog(question, choices, allow_custom, multi_select, parent=self)
        except Exception as e:
            # Never let a widget-construction failure bubble up into the
            # worker: it would be swallowed by AgentWorker.run()'s blanket
            # except and leave the ask_user tool call permanently unanswered,
            # stalling the turn with no visible explanation.
            self.chat_panel.add_message(ChatMessage.create(
                "tool_error", f"Could not build the ask_user dialog: {e}",
                role_label="Error", min_verbosity=0,
            ))
            return None
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.result_text or None

    def _send_btn_disabled_relaunch(self):
        self._lock_buttons_for_turn()
        QTimer.singleShot(30, self._launch_worker)

    # Kinds that represent agent-side output in the chat view. A "retry"
    # removes the trailing run of these (the agent's last response block) and
    # regenerates from the last user message. "user" is the boundary we stop
    # at; everything non-user after it is part of the regenerable block.
    _AGENT_SIDE_KINDS = frozenset({
        "assistant", "reasoning", "thought",
        "tool_call", "tool_result", "tool_error",
        "system", "time_barrier",
    })

    def _rebuild_agent_history_from_panel(self):
        """Rebuild agent.messages directly from the chat panel.

        WARNING: This method strips ALL tool protocol messages (tool calls,
        tool results, tool errors) from the raw history because the chat
        panel only stores them as display-only bubbles. It must therefore
        NEVER be called in the normal Respond / Retry / Continue flow, or
        every tool result from every prior turn is flushed from the model's
        context.

        It is kept only as a manual recovery tool for the edge case where
        the raw history has become irreparably desynced from the panel.
        """
        if not self.agent:
            return
        conv: list = []
        for m in self.chat_panel.messages:
            if not m.in_context:
                continue
            if m.kind == "user":
                conv.append({"role": "user", "content": m.text})
            elif m.kind in edit_tracking.ASSISTANT_BUBBLE_KINDS:
                conv.append({"role": "assistant", "content": m.text})
        self.agent.rebuild_history(conv)

    def _begin_regenerate_turn(self):
        """Shared preamble for Retry / Respond: arm the streaming state and
        launch a worker WITHOUT adding a new user message. The caller is
        responsible for having already trimmed the chat panel and the agent's
        message history to the correct point (see _on_retry_clicked /
        _on_force_clicked).

        The raw history is already in sync at this point:
        * Edits are written back by _on_message_edited (edit_tracking).
        * Rewinds are written back by _on_rewind_to (edit_tracking + import).
        * Retry's trailing-block trim is done by prepare_regenerate().
        * Respond does not modify the history at all (prepare_respond only
          lifts the _turn_complete guard).
        Rebuilding from the panel here would destroy every tool call/result
        message in the history — the panel only stores them as display-only
        bubbles — so it must NOT be done.
        """
        self._pending_prompt_summary = "regenerate"
        self._stream_char_count = 0
        self._chunk_count = 0
        self._perf_start_time = None
        self._perf_content_chunks = 0
        self._live_content_msg = None
        self._live_reasoning_msg = None
        self._had_live_reasoning_this_round = False
        self._had_live_content_this_round = False
        self._end_turn_summary_shown = False
        self._streaming_to_chat = self.agent_policy_dict.get("stream_to_chat", True)
        self._autosave_current_session()
        self._lock_buttons_for_turn()
        QTimer.singleShot(30, self._launch_worker)

    def _lock_buttons_for_turn(self):
        """Disable every way of starting a second turn while one is running."""
        self.send_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.force_btn.setEnabled(False)
        self.continue_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

    def _on_retry_clicked(self):
        """Retry: remove the agent's last response block and regenerate it.

        Only meaningful when the most recent chat message is an agent-side
        message (i.e. there is a response to regenerate). If the last message
        is a user message, there is nothing to remove — that case is what
        "Respond" is for, so we no-op here rather than surprise the user.
        """
        if not self.agent:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.runtime.state() != "running":
            QMessageBox.information(self, "Server not running", "The llama-server is not running. Start it first via the Start Server button.")
            return

        # Only retry when the last message is agent-side output to regenerate.
        if not self.chat_panel.messages:
            return
        if self.chat_panel.messages[-1].kind == "user":
            return

        # Remove the trailing agent-side block from the chat view (mirrors
        # what agent.prepare_regenerate() does to the raw message history).
        removed_any = False
        while (self.chat_panel.messages
               and self.chat_panel.messages[-1].kind in self._AGENT_SIDE_KINDS):
            removed_any = bool(self.chat_panel.remove_last_message())
            if not removed_any:
                break

        # Trim the agent's raw history and re-arm the turn guard so step()
        # reaches the model. If there's no user message left to answer to
        # (e.g. the conversation was empty), bail without launching.
        if not self.agent.prepare_regenerate():
            # We removed chat messages but the agent has nothing to answer —
            # this should only happen in a degenerate empty-conversation case.
            self._autosave_current_session()
            self._update_retry_force_buttons()
            return

        self._begin_regenerate_turn()

    def _on_force_clicked(self):
        """Respond: force the agent to answer the last message even if the
        input box is empty. Intended for the roleplay case where the user's
        message is the last thing in the conversation and the agent has not
        (yet) replied.

        No chat message is removed and no raw message is trimmed — the user's
        message stays, and so does anything the agent already said. Use Retry to
        replace an existing reply, or Continue to extend one.
        """
        if not self.agent:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.runtime.state() != "running":
            QMessageBox.information(self, "Server not running", "The llama-server is not running. Start it first via the Start Server button.")
            return

        # Nothing is deleted: prepare_respond only lifts the "turn finished"
        # guard so step() reaches the model again.
        if not self.agent.prepare_respond():
            return

        self._begin_regenerate_turn()

    def _on_continue_clicked(self):
        """Continue: append to the agent's last message instead of replacing it.

        The model is asked to keep writing the message that is already there, so
        the new words stream into the SAME chat bubble. This is the counterpart
        to Retry — Retry replaces the reply, Continue extends it — and it is what
        makes "put words into the agent's mouth" work: edit the last assistant
        message first (right-click -> Edit), then Continue picks up exactly the
        text on screen.
        """
        if not self.agent:
            QMessageBox.information(self, "No project", "Open a project first.")
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if self.runtime.state() != "running":
            QMessageBox.information(self, "Server not running", "The llama-server is not running. Start it first via the Start Server button.")
            return

        anchor_idx = self.agent.continue_anchor_index()
        if anchor_idx is None:
            self.chat_panel.add_message(ChatMessage.create(
                "system",
                "Nothing to continue — the agent's last message is a tool call or "
                "is empty. Use Respond to answer the last message, or Retry to "
                "regenerate it.",
                color="#d19900", in_context=False, min_verbosity=1,
            ))
            return

        # Route the new words into the bubble that corresponds to the message
        # the agent actually chose to continue. Preferring "the last assistant
        # bubble on screen" looks equivalent and usually is, but the panel can
        # carry trailing agent-side bubbles with no raw twin of their own (an
        # end_turn summary, a local notice), and then the two disagree — the
        # model continues one message while the UI paints onto another.
        target = None
        panel_kinds = [m.kind for m in self.chat_panel.messages]
        panel_texts = [m.text for m in self.chat_panel.messages]
        for b, kind in enumerate(panel_kinds):
            if kind not in edit_tracking.ASSISTANT_BUBBLE_KINDS:
                continue
            if self.chat_panel.messages[b].streaming:
                continue
            idx, _why = edit_tracking.resolve_bubble(
                self.agent.messages, panel_kinds, panel_texts, b)
            if idx == anchor_idx:
                target = self.chat_panel.messages[b]
                break
        # Whatever the panel shows wins, so an edit you just made is exactly the
        # text the model is asked to keep writing. prepare_continue() accepts
        # prefix_text and rewrites the anchor message to match, so the model's
        # prefix and the on-screen text cannot diverge.
        prefix_text = target.text if target is not None else None

        # Do NOT call _rebuild_agent_history_from_panel() here: it would strip
        # every tool call/result from the raw history. The raw history is
        # already in sync (edits via _on_message_edited, rewinds via
        # _on_rewind_to), and prefix_text above handles the on-screen override.
        prefix = self.agent.prepare_continue(prefix_text)
        if not prefix or not prefix.strip():
            self.agent.clear_continuation()
            self.chat_panel.add_message(ChatMessage.create(
                "system",
                "Nothing to continue — the agent's last message is empty.",
                color="#d19900", in_context=False, min_verbosity=1,
            ))
            self._update_retry_force_buttons()
            return

        # Route the live deltas into the existing bubble. Marking it streaming
        # again is what makes append_to_message repaint it, and it is finalised
        # by _on_step_finished exactly like a normal streamed reply.
        if target is not None:
            target.streaming = True
            self._live_content_msg = target
            self.chat_panel.refresh_message(target)
            # Content is already on screen, so suppress the end-of-turn
            # "add a bubble for the final text" fallback entirely.
            self._had_live_content_this_round = True

        self._pending_prompt_summary = "continue"
        self._stream_char_count = 0
        self._chunk_count = 0
        self._perf_start_time = None
        self._perf_content_chunks = 0
        self._live_reasoning_msg = None
        self._had_live_reasoning_this_round = False
        self._end_turn_summary_shown = False
        self._streaming_to_chat = self.agent_policy_dict.get("stream_to_chat", True)
        self._autosave_current_session()
        self._lock_buttons_for_turn()
        QTimer.singleShot(30, self._launch_worker)

    # ChatMessage kinds that belong to a deployed subagent, not to this
    # conversation's own agent. Keyed off by _on_delta to route worker deltas
    # into their own bubbles (see core/agent.py::_subagent_stream_cb for why
    # these phases arrive un-remapped).
    SUBAGENT_PHASE_CONTENT = "subagent_content"
    SUBAGENT_PHASE_REASONING = "subagent_reasoning"
    SUBAGENT_PHASE_TOOL_ARGS = "subagent_tool_args"
    SUBAGENT_PHASES = frozenset({
        SUBAGENT_PHASE_CONTENT, SUBAGENT_PHASE_REASONING, SUBAGENT_PHASE_TOOL_ARGS,
    })

    def _append_subagent_delta(self, phase: str, piece: str, tool_name: str = ""):
        """Append one streamed delta from a deployed subagent to its own bubble.

        Three independent ChatMessage objects, mirroring how the parent's own
        content/reasoning/tool_args are handled, so a worker's thinking stays
        separate from its prose and both stay separate from the agent's. The
        kinds are display-only (in_context=False): the worker's intermediate
        output never enters this conversation's raw history — only its final
        answer does, inside the deploy_subagent tool result — and keeping them
        out of the paired kinds is what stops them from shifting the
        bubble<->raw message pairing used by edits/rewinds (core/edit_tracking).

        Honours the "stream response text into chat" setting the same way the
        parent does: with streaming into chat switched off, nothing is appended
        here and the deployment still shows its tool call/result trace, which is
        exactly what the parent's own text does in that mode.
        """
        if not self._streaming_to_chat:
            return
        if phase == self.SUBAGENT_PHASE_CONTENT:
            if self._live_subagent_reasoning_msg is not None:
                self.chat_panel.finalize_message(self._live_subagent_reasoning_msg)
                self._live_subagent_reasoning_msg = None
            if self._live_subagent_content_msg is None:
                self._live_subagent_content_msg = ChatMessage.create(
                    "subagent", "", streaming=True)
                self.chat_panel.add_message(self._live_subagent_content_msg)
            self.chat_panel.append_to_message(self._live_subagent_content_msg, piece)
        elif phase == self.SUBAGENT_PHASE_REASONING:
            if self._live_subagent_reasoning_msg is None:
                self._live_subagent_reasoning_msg = ChatMessage.create(
                    "subagent_reason", "", streaming=True)
                self.chat_panel.add_message(self._live_subagent_reasoning_msg)
            self.chat_panel.append_to_message(self._live_subagent_reasoning_msg, piece)
        elif phase == self.SUBAGENT_PHASE_TOOL_ARGS:
            # The worker's tool calls already surface as ordinary tool_call
            # bubbles via _on_tool_event (the runner emits the same events the
            # parent does), so streaming its raw argument JSON as a *second*
            # bubble would double the trace. Nothing to do here; kept explicit
            # so the phase is not silently swallowed if that ever changes.
            return

    def _on_delta(self, piece: str, phase: str, tool_name: str = "", tokens: int = None, round_num: int = 0):
        # ---- subagent boundary markers -------------------------------------
        # Emitted by Agent._deploy_subagent around a deployment. They carry no
        # text and exist only so the UI can close the parent's live bubble
        # before the worker speaks and again after it stops. They are handled
        # BEFORE the round_num reset below and deliberately do not touch
        # _last_delta_round: a deployment happens mid-round (round_num is bumped
        # once per parent model call), and stealing the reset here would leave
        # the parent's post-deployment deltas looking like a brand-new round to
        # every later branch while this one returns early.
        if phase == "subagent_begin":
            # Whatever the parent was writing is finished as a *live* bubble:
            # the worker's output must never continue it.
            if self._live_content_msg is not None:
                self.chat_panel.finalize_message(self._live_content_msg)
                self._live_content_msg = None
            if self._live_reasoning_msg is not None:
                self.chat_panel.finalize_message(self._live_reasoning_msg)
                self._live_reasoning_msg = None
            live_tool_msg = getattr(self, "_live_tool_call_msg", None)
            if live_tool_msg is not None:
                self.chat_panel.finalize_message(live_tool_msg)
                self._live_tool_call_msg = None
            # Fresh worker bubbles for this deployment, appended in order after
            # everything already on screen — i.e. its own bubble, not the
            # parent's.
            self._live_subagent_content_msg = None
            self._live_subagent_reasoning_msg = None
            self._live_subagent_tool_msg = None
            self._subagent_block_open = True
            return
        if phase == "subagent_end":
            for attr in ("_live_subagent_content_msg",
                         "_live_subagent_reasoning_msg",
                         "_live_subagent_tool_msg"):
                msg = getattr(self, attr, None)
                if msg is not None:
                    self.chat_panel.finalize_message(msg)
                    setattr(self, attr, None)
            # Agent._deploy_subagent also bumps its response round, so the next
            # parent delta arrives under a new round_num and takes the reset
            # branch below — which clears these again and re-arms the per-round
            # flags. Belt and braces: this line makes the boundary correct even
            # if the bump ever stops happening.
            self._subagent_block_open = False
            self._had_live_content_this_round = False
            self._had_live_reasoning_this_round = False
            return
        # A parent delta after a deployment must start its own bubble rather
        # than resume the worker's, and must not be swallowed by the round_num
        # guard below (the worker's own deltas never advance round_num).
        if (phase in ("content", "reasoning", "tool_args")
                and getattr(self, "_subagent_block_open", False)
                and round_num == getattr(self, "_last_delta_round", None)):
            self._last_delta_round = None
        if round_num != getattr(self, "_last_delta_round", None):
            self._last_delta_round = round_num
            self._stream_char_count = 0
            self._chunk_count = 0
            self._had_live_reasoning_this_round = False
            self._had_live_content_this_round = False
            # Finalize and drop the previous round's live tool-call message.
            # Without this, a new round's streamed tool_args get silently
            # appended onto the PREVIOUS round's already-finished tool-call
            # bubble, because step() runs every round internally before
            # ever returning to the UI — _on_step_finished (which normally
            # clears this) doesn't fire between rounds, only at the very end.
            prev_tool_msg = getattr(self, "_live_tool_call_msg", None)
            if prev_tool_msg is not None:
                self.chat_panel.finalize_message(prev_tool_msg)
                self._live_tool_call_msg = None
        if tokens is not None and phase == "usage":
            # usage arrives once decode has produced tokens — prompt
            # processing is done, so the prefill-progress overlay no longer
            # applies and we mustn't keep re-asserting "Reading... (x%)".
            self._in_reading_state = False
            self.activity_label.setText(f"Thinking... ({tokens} tokens)")
            self.activity_label.setStyleSheet("color:#5591c7; font-style: italic;")
            return
        if tokens is not None and phase == "usage_total":
            return
        if phase == "subagent_usage_total":
            # The worker's own context occupancy. Consumed by the Agent
            # (core/agent.py::_subagent_stream_cb) and read by the Context tab's
            # subagent meter off the agent object — it never reaches the chat, so
            # bail out here rather than falling through to the content branches
            # below (which would render the number as a "Thinking..." label).
            return
        if phase == "timings" and piece:
            try:
                timings = json.loads(piece)
                parts = []
                if timings.get("prompt_per_second") is not None:
                    parts.append(f"prompt: {timings['prompt_per_second']:.1f} tok/s")
                if timings.get("predicted_per_second") is not None:
                    parts.append(f"decode: {timings['predicted_per_second']:.1f} tok/s")
                if parts:
                    self.perf_label.setText("⚡ " + ", ".join(parts))
                    self.perf_label.setVisible(True)
                self._append_log(
                    f"[Performance] prompt_per_second={timings.get('prompt_per_second')} "
                    f"predicted_per_second={timings.get('predicted_per_second')} "
                    f"prompt_ms={timings.get('prompt_ms')} "
                    f"predicted_ms={timings.get('predicted_ms')} "
                    f"prompt_per_token_ms={timings.get('prompt_per_token_ms')} "
                    f"predicted_per_token_ms={timings.get('predicted_per_token_ms')}"
                )
            except (json.JSONDecodeError, TypeError):
                pass
            return

        # ---- Deployed subagent: its own bubbles, never the parent's ---------
        # The worker's deltas arrive tagged 'subagent_*' (see
        # core/agent.py::_subagent_stream_cb). They used to be relabelled as
        # ordinary 'content'/'reasoning'/'tool_args', which made them fall into
        # the branches below and append straight onto the *parent's* live
        # bubble — the parent's text and the worker's text merged into one
        # message. Routing them to dedicated ChatMessage objects is what gives
        # a deployment its own visible block, and it is what lets the parent
        # start a fresh bubble again afterwards.
        if tokens is None and phase in self.SUBAGENT_PHASES:
            self._append_subagent_delta(phase, piece, tool_name)
            return

        # ---- Live streaming: reasoning and content are independent
        # ChatMessage objects, created the moment their FIRST delta arrives.
        # This is what makes thoughts render live and in correct order —
        # whichever phase's first delta arrives first gets its bubble first,
        # naturally, with no buffering/reordering logic needed.
        if self._streaming_to_chat and tokens is None:
            if phase == "reasoning":
                if self._live_reasoning_msg is None:
                    self._live_reasoning_msg = ChatMessage.create("reasoning", "", streaming=True)
                    self.chat_panel.add_message(self._live_reasoning_msg)
                self.chat_panel.append_to_message(self._live_reasoning_msg, piece)
                self._had_live_reasoning_this_round = True
            elif phase == "content":
                if self._live_content_msg is None:
                    self._live_content_msg = ChatMessage.create("assistant", "", streaming=True)
                    self.chat_panel.add_message(self._live_content_msg)
                self.chat_panel.append_to_message(self._live_content_msg, piece)
                self._had_live_content_this_round = True

                import time as _time
                if self._perf_start_time is None:
                    self._perf_start_time = _time.time()
                self._perf_content_chunks += 1
                elapsed = _time.time() - self._perf_start_time
                if elapsed > 0.01:
                    live_tps = self._perf_content_chunks / elapsed
                    if live_tps > 0:
                        self.perf_label.setText(f"⚡ decode: {live_tps:.1f} tok/s")
                        self.perf_label.setVisible(True)
            elif phase == "tool_args":
                # Finalize whichever live messages exist so far — each on
                # ITS OWN widget, no searching, nothing left behind.
                if self._live_content_msg is not None:
                    self.chat_panel.finalize_message(self._live_content_msg)
                    self._live_content_msg = None
                if self._live_reasoning_msg is not None:
                    self.chat_panel.finalize_message(self._live_reasoning_msg)
                    self._live_reasoning_msg = None

                label = tool_name or "tool call"
                display_piece = piece
                try:
                    parsed = json.loads(piece)
                    if isinstance(parsed, dict):
                        display_piece = _format_tool_args(parsed)
                except (json.JSONDecodeError, ValueError):
                    pass

                live_key = "_live_tool_call_msg"
                live_msg = getattr(self, live_key, None)
                if live_msg is None:
                    live_msg = ChatMessage.create(
                        "tool_call", display_piece, streaming=True,
                        role_label=f"Tool call (building: {label})",
                    )
                    setattr(self, live_key, live_msg)
                    self.chat_panel.add_message(live_msg)
                else:
                    self.chat_panel.append_to_message(live_msg, display_piece)

        self._stream_char_count = getattr(self, "_stream_char_count", 0) + len(piece)
        self._chunk_count = getattr(self, "_chunk_count", 0) + 1
        est_chars = self._stream_char_count
        n_chunks = self._chunk_count

        if self.agent is not None and phase in ("content", "reasoning", "tool_args"):
            self.agent.last_known_real_tokens = max(
                self.agent.last_known_real_tokens or 0,
                self.agent.current_context_tokens() + self._chunk_count,
            )

        if phase == "reasoning":
            text, color = f"Thinking... ({est_chars} chars, {n_chunks} chunks)", "#5591c7"
        elif phase == "tool_args":
            label = tool_name or "tool call"
            text, color = f"Building tool call: {label}... ({est_chars} chars, {n_chunks} chunks)", "#d19900"
        elif phase == "content":
            text, color = f"Writing response... ({est_chars} chars, {n_chunks} chunks)", "#cdeecb"
        else:
            text, color = f"Thinking... ({est_chars} chars, {n_chunks} chunks)", "#5591c7"
        # Any streamed decode delta means prefill is over — stop showing the
        # prompt-progress percentage so it can't flicker back over the
        # live "Thinking..."/"Writing..." text on a late-arriving line.
        self._in_reading_state = False
        self.activity_label.setText(text)
        self.activity_label.setStyleSheet(f"color:{color}; font-style: italic;")

    def _on_web_fetch_truncated(self, info: dict):
        url = info.get("url", "(unknown URL)")
        trunc_mode = self.web_fetch_policy_dict.get("truncation_mode", "show_popup")

        if trunc_mode == "silent":
            return

        if trunc_mode == "deny":
            self.chat_panel.add_message(ChatMessage.create(
                "tool_result",
                f"Fetch of '{url}' was truncated — content exceeded configured limits. "
                f"Increase limits in Settings > Fetch Limits if needed.",
                role_label="Tool result",
            ))
            return

        truncated_reasons = ["page content exceeded configured limits"]
        if self.agent:
            for msg in reversed(self.agent.messages):
                content = msg.get("content", "")
                if isinstance(content, str) and "[FETCH_TRUNCATED:" in content:
                    marker = "[FETCH_TRUNCATED: truncated — page exceeded limit ("
                    if marker in content:
                        end_paren = content.find(") — ", content.find(marker) + len(marker))
                        if end_paren >= 0:
                            reason_text = content[content.find(marker) + len(marker):end_paren]
                            truncated_reasons = [r.strip() for r in reason_text.split(" and ")]
                    break
        dialog = WebFetchTruncationDialog(url, truncated_reasons, self)
        dialog.exec()
        choice = dialog.choice
        if choice == "increase":
            self.chat_panel.add_message(ChatMessage.create(
                "system", "Increase limits and re-fetch selected — ask the agent to retry with higher limits.",
                color="#d19900", in_context=False, min_verbosity=1,
            ))
        elif choice == "cancel":
            self._cancel_turn()

    def _on_worker_failed(self, message: str):
        self.chat_panel.add_message(ChatMessage.create("tool_error", message, role_label="Error"))
        self._finish_turn_ui()

    def _on_step_finished(self, result):
        # Finalize any still-open live streaming messages up front, on every
        # code path — including all the early-return branches below. This
        # is the direct replacement for the old unconditional
        # _close_stream_div() call, and it can't leak because finalize
        # targets an exact widget reference rather than searching for one.
        if self._live_content_msg is not None:
            self.chat_panel.finalize_message(self._live_content_msg)
            self._live_content_msg = None
        if self._live_reasoning_msg is not None:
            self.chat_panel.finalize_message(self._live_reasoning_msg)
            self._live_reasoning_msg = None
        live_tool_msg = getattr(self, "_live_tool_call_msg", None)
        if live_tool_msg is not None:
            self.chat_panel.finalize_message(live_tool_msg)
            self._live_tool_call_msg = None
        # The worker's bubbles too. Normally "subagent_end" already did this,
        # but a cancellation or an error can abandon a deployment mid-stream,
        # and a bubble left streaming repaints as "● live" forever.
        for attr in ("_live_subagent_content_msg",
                     "_live_subagent_reasoning_msg",
                     "_live_subagent_tool_msg"):
            msg = getattr(self, attr, None)
            if msg is not None:
                self.chat_panel.finalize_message(msg)
                setattr(self, attr, None)
        self._subagent_block_open = False

        if result.web_browser_kill_request:
            req = result.web_browser_kill_request
            dlg = WebBrowserKillDialog(req["browser_type"], self)
            dlg.exec()
            if dlg.choice == "kill":
                browser_type = req["browser_type"]
                if browser_type == "playwright":
                    from core.browser_fetcher import kill_active_browser as _kill_pw
                    killed = _kill_pw()
                    if killed:
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", "Killed active Playwright browser — a fresh instance will be started.",
                            color="#d19900", in_context=False, min_verbosity=1,
                        ))
                elif browser_type == "selenium":
                    from core.browser_connector import kill_active_chrome as _kill_sc
                    killed = _kill_sc()
                    if killed:
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", "Killed active Selenium Chrome instance — a fresh instance will be started.",
                            color="#d19900", in_context=False, min_verbosity=1,
                        ))
                else:
                    self.chat_panel.add_message(ChatMessage.create(
                        "system", f"Active {browser_type} browser killed.",
                        color="#d19900", in_context=False, min_verbosity=1,
                    ))
            else:
                self.chat_panel.add_message(ChatMessage.create(
                    "system", "Web operation skipped (browser kill cancelled).",
                    in_context=False, min_verbosity=1,
                ))
            self._finish_turn_ui()
            self.send_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            QTimer.singleShot(30, self._launch_worker)
            return

        if result.overflow_choice_needed:
            dialog = OverflowChoiceDialog(result.overflow_choice_needed, self)
            dialog.exec()
            choice = dialog.choice
            if choice == "abort":
                self.chat_panel.add_message(ChatMessage.create(
                    "system", "Turn aborted due to context limit.", color="#e05a5a",
                    in_context=False, min_verbosity=1,
                ))
                self._finish_turn_ui()
                return
            self.agent.resolve_overflow_choice(choice)
            self.context_tab.refresh()
            self.send_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            QTimer.singleShot(30, self._launch_worker)
            return

        if result.ask_user_request:
            self.chat_panel.add_message(ChatMessage.create(
                "assistant", result.assistant_text or "(asking a question)",
            ))
            req = result.ask_user_request
            answer = self._prompt_ask_user(
                req["question"], req.get("choices") or [],
                req.get("allow_custom_answer", True), req.get("multi_select", False),
            )
            if answer is None:
                # The user dismissed the dialog. resolve_ask_user MUST still be
                # called or the ask_user tool call stays unanswered in the
                # history, which orphans the protocol and breaks the next
                # request. Say so honestly instead of inventing an answer, and
                # end the turn rather than relaunching the worker to keep
                # nagging the user.
                self.agent.resolve_ask_user(
                    "[The user dismissed this question without answering. "
                    "Do not ask it again — state the assumption you are making "
                    "and continue, or stop and wait for their next message.]"
                )
                self.chat_panel.add_message(ChatMessage.create(
                    "system", "Question dismissed without an answer.",
                    color="#d19900", in_context=False, min_verbosity=1,
                ))
                self._autosave_current_session()
                self._finish_turn_ui()
                return
            self.agent.resolve_ask_user(answer)
            self.chat_panel.add_message(ChatMessage.create("user", answer))
            self._send_btn_disabled_relaunch()
            return

        if result.script_approval_request:
            req = result.script_approval_request
            args_str = " ".join(req["args"]) if req["args"] else "(none)"
            reply = QMessageBox.question(
                self, "Approve script execution?",
                f"The agent wants to run:\n\n  python {req['path']} {args_str}\n\n"
                f"Timeout: {req['timeout']}s\n\nAllow this?",
                QMessageBox.Yes | QMessageBox.No,
            )
            self.agent.resolve_script_approval(
                approved=(reply == QMessageBox.Yes),
                path=req["path"], args=req["args"], call_id=req["call_id"], timeout=req["timeout"],
            )
            if reply == QMessageBox.Yes:
                self.chat_panel.add_message(ChatMessage.create("user", "Execution was approved"))
            else:
                self.chat_panel.add_message(ChatMessage.create("user", "Execution was denied"))

        if result.command_approval_request:
            req = result.command_approval_request
            from ui.command_approval_dialog import CommandApprovalDialog
            allowlist = self.command_policy_dict.get("allowlist", [])
            all_cmds = req.get("all_commands")
            if not all_cmds:
                cmd_str = req.get("command", "")
                if self.executor:
                    all_cmds = self.executor.parse_shell_commands(cmd_str)
                else:
                    import shlex
                    try:
                        tokens = shlex.split(cmd_str) if cmd_str else []
                    except ValueError:
                        tokens = cmd_str.split() if cmd_str else []
                    ops = {"&&", "||", ";"}
                    all_cmds = []
                    current = []
                    for tok in tokens:
                        if tok in ops or tok == "|":
                            if current:
                                all_cmds.append(current[0])
                                current = []
                        else:
                            current.append(tok)
                    if current:
                        all_cmds.append(current[0])
            dlg = CommandApprovalDialog(
                command=req["command"],
                description=req["description"],
                timeout=req["timeout"],
                parent=self,
                all_commands=all_cmds,
                allowlist=allowlist,
            )

            def on_approved(approved, cmd_name):
                local_allowlist = dlg.get_local_allowlist()
                existing_allowlist = self.command_policy_dict.get("allowlist", [])
                existing_cmds = {e.get("command", "").lower() for e in existing_allowlist}
                for cmd_lower in local_allowlist:
                    if cmd_lower not in existing_cmds:
                        for cmd in req.get("all_commands", []):
                            if cmd.lower() == cmd_lower:
                                existing_allowlist.append({"command": cmd, "description": req["description"]})
                                existing_cmds.add(cmd_lower)
                                break
                self.command_policy_dict["allowlist"] = existing_allowlist
                self.app_settings.save_command_policy(self.command_policy_dict)
                self.agent.policy.command_allowlist = existing_allowlist

                self.agent.resolve_command_approval(
                    approved=approved,
                    command=req["command"],
                    description=req["description"],
                    call_id=req["call_id"],
                    timeout=req["timeout"],
                    deny_reason=dlg.get_deny_reason(),
                    added_to_allowlist=False,
                )
                if approved:
                    self.chat_panel.add_message(ChatMessage.create("user", "Command execution was approved"))
                else:
                    reason = dlg.get_deny_reason()
                    self.chat_panel.add_message(ChatMessage.create(
                        "user", f"Command execution was denied{f': {reason}' if reason else ''}"
                    ))
                self.send_btn.setEnabled(False)
                self.cancel_btn.setEnabled(True)
                QTimer.singleShot(30, self._launch_worker)

            def on_denied(deny_reason):
                self.agent.resolve_command_approval(
                    approved=False,
                    command=req["command"],
                    description=req["description"],
                    call_id=req["call_id"],
                    timeout=req["timeout"],
                    deny_reason=deny_reason,
                    added_to_allowlist=False,
                )
                self.chat_panel.add_message(ChatMessage.create(
                    "user", f"Command execution was denied: {deny_reason}"
                ))
                self.send_btn.setEnabled(False)
                self.cancel_btn.setEnabled(True)
                QTimer.singleShot(30, self._launch_worker)

            dlg.accepted.connect(on_approved)
            dlg.denied.connect(on_denied)
            dlg.exec()

        if result.error:
            self.chat_panel.add_message(ChatMessage.create("tool_error", result.error, role_label="Error"))
            self._finish_turn_ui()
            return

        if result.cancelled:
            self.chat_panel.add_message(ChatMessage.create(
                "system", "(turn cancelled)", in_context=False, min_verbosity=1,
            ))
            if result.pending_patches:
                self.patch_panel.add_patches(result.pending_patches)
                self._update_change_queue_badge()
            self._refresh_file_list()
            self._save_autosave_chat()
            self._autosave_current_session()
            self._finish_turn_ui()
            return

        verbosity_name = self.agent_policy_dict.get("verbosity", "Full trace")
        if verbosity_name == "Tool summary":
            for entry in result.tool_log:
                self.chat_panel.add_message(ChatMessage.create(
                    "system", f"tool: {entry}", in_context=False, min_verbosity=1,
                ))

        # ---- Reasoning-before-content ordering ----
        # If reasoning arrived but was never streamed live this turn (e.g.
        # use_streaming=False, or the server sent it all at once in the
        # final response rather than as deltas), create it now and insert it
        # BEFORE the assistant-text message that follows — matching your
        # request that thoughts appear before the answer, not after.
        # Use the per-round flags captured DURING streaming, not the
        # _live_*_msg refs (already cleared to None by the finalize block
        # at the top of this method). This is what actually distinguishes
        # "already streamed live this round" from "never streamed" —
        # the bug was relying on refs that are unconditionally None by now.
        if result.reasoning_text.strip() and not self._had_live_reasoning_this_round:
            self.chat_panel.add_message(ChatMessage.create("reasoning", result.reasoning_text))

        if result.assistant_text.strip() and not self._end_turn_summary_shown:
            if self._streaming_to_chat and not self._had_live_content_this_round:
                self.chat_panel.add_message(ChatMessage.create("assistant", result.assistant_text))
            elif not self._streaming_to_chat:
                self.chat_panel.add_message(ChatMessage.create("assistant", result.assistant_text))

        if not result.assistant_text.strip() and result.ended:
            self.chat_panel.add_message(ChatMessage.create(
                "tool_result", "Turn ended by agent.", role_label="Tool result",
            ))

        if result.overflow_triggered:
            self.chat_panel.add_message(ChatMessage.create(
                "system", "(context usage action triggered automatically — see Context tab)",
                color="#e0c95a", in_context=False, min_verbosity=1,
            ))
            self.context_tab.refresh()

        if result.pending_patches:
            # patch.status-aware message: no longer says "staged for review"
            # for patches that were actually auto-applied under a permissive
            # patch_mode.
            awaiting_review = sum(1 for p in result.pending_patches if p.status == "pending")
            auto_applied = len(result.pending_patches) - awaiting_review
            if awaiting_review and auto_applied:
                msg_text = f"{awaiting_review} change(s) staged for review; {auto_applied} applied automatically."
            elif awaiting_review:
                msg_text = f"{awaiting_review} change(s) staged for review."
            else:
                msg_text = f"{auto_applied} change(s) applied automatically."
            self.chat_panel.add_message(ChatMessage.create(
                "system", msg_text, in_context=False, min_verbosity=1,
            ))
            self.patch_panel.add_patches(result.pending_patches)
            self._update_change_queue_badge()

        self._refresh_file_list()
        self._save_autosave_chat()
        self._autosave_current_session()
        self.context_tab.refresh()
        self._finish_turn_ui()

    def _update_retry_force_buttons(self):
        """Enable/disable Retry / Respond / Continue to match the current
        turn state.

        All three are only meaningful when a worker is NOT running (we can't
        regenerate mid-turn) and a project is open. While a turn is in flight
        they are disabled; once the turn settles they are re-enabled so the
        user can act on the fresh last message.

        Continue is greyed out beyond that when there is genuinely nothing to
        extend — the agent has no plain-text assistant message to append to
        (e.g. its last output was only a tool call). The tooltip explains why,
        so the disabled state is self-documenting rather than mysterious.
        """
        busy = self.worker is not None and self.worker.isRunning()
        can_use = bool(self.agent) and not busy
        self.retry_btn.setEnabled(can_use)
        self.force_btn.setEnabled(can_use)

        can_continue = False
        if can_use:
            try:
                # The raw history is authoritative (edits are synced into it),
                # and the panel can trail it with local notices that have no raw
                # twin, so the raw check is the one worth trusting here.
                can_continue = self.agent.continuation_target() is not None
            except Exception:
                can_continue = False
        if can_use and can_continue:
            try:
                # A message can be continuable in the raw history yet have no
                # bubble on screen (restored session, "Chat only" history).
                # Pointing the user at Respond/Retry beats continuing into a
                # bubble that does not exist.
                anchor = self.agent.continue_anchor_index()
                panel_kinds = [m.kind for m in self.chat_panel.messages]
                panel_texts = [m.text for m in self.chat_panel.messages]
                shown = any(
                    k in edit_tracking.ASSISTANT_BUBBLE_KINDS
                    and edit_tracking.resolve_bubble(
                        self.agent.messages, panel_kinds, panel_texts, b)[0] == anchor
                    for b, k in enumerate(panel_kinds)
                )
                can_continue = bool(shown)
            except Exception:
                can_continue = True
        self.continue_btn.setEnabled(can_continue)
        if can_use and not can_continue:
            self.continue_btn.setToolTip(
                "Nothing to continue: the agent's last message is a tool call, is "
                "empty, or has no bubble in the chat. Use Respond to answer the "
                "last message, or Retry to regenerate it."
            )
        else:
            self.continue_btn.setToolTip(
                "Continue the agent's last message from where it ends, appending to "
                "that same message instead of starting a new one.\n\n"
                "To put words into the agent's mouth: right-click its last message -> "
                "Edit, write how it should sound so far, then press Continue."
            )

    def _finish_turn_ui(self):
        self._in_reading_state = False
        self._prompt_progress = None
        self._initial_prompt_progress = None
        self.activity_label.setText("Idle")
        self.activity_label.setStyleSheet("color:#8a8a8a; font-style: italic;")
        self.perf_label.setText("")
        self.perf_label.setVisible(False)
        self.send_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._update_retry_force_buttons()
        self.worker = None
        self._stream_char_count = 0
        self._chunk_count = 0
        self._perf_start_time = None
        self._perf_content_chunks = 0
        self._live_content_msg = None
        self._live_reasoning_msg = None
        self._live_tool_call_msg = None
        for attr in ("_live_subagent_content_msg",
                     "_live_subagent_reasoning_msg",
                     "_live_subagent_tool_msg"):
            setattr(self, attr, None)
        self._subagent_block_open = False
        self._had_live_reasoning_this_round = False
        self._had_live_content_this_round = False
        self._end_turn_summary_shown = False
        self._streaming_to_chat = False

    def _on_patches_decided(self, decisions):
        decisions_by_path = {}
        for d in decisions:
            decisions_by_path.setdefault(d["patch"].path, []).append(d)

        changed_files_for_snapshot = {}
        for path, path_decisions in decisions_by_path.items():
            approved_count = sum(1 for d in path_decisions if d["approved"])
            total_count = len(path_decisions)

            if approved_count == total_count:
                if self.draft_store and self.draft_store.has_draft(path):
                    # accept_all() no longer clears the draft itself — clear
                    # it ONLY once write_file has actually succeeded, and
                    # never let a failure here propagate uncaught, since an
                    # uncaught exception previously killed processing for
                    # every OTHER path still waiting in this same batch too.
                    draft_content = self.draft_store.accept_all(path)
                    try:
                        if draft_content is not None:
                            self.executor.workspace.write_file(path, draft_content)
                        self.draft_store.clear(path)
                        for d in path_decisions:
                            d["patch"].status = "accepted"
                        prev = ""
                        try:
                            prev = self.executor.workspace.read_file(path)
                        except WorkspaceError:
                            pass
                        changed_files_for_snapshot[path] = prev
                    except Exception as e:
                        # Draft deliberately left intact (not cleared) so the
                        # approved content isn't lost — mark failed instead
                        # of silently vanishing.
                        for d in path_decisions:
                            d["patch"].status = "failed"
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", f"Failed to apply {path}: {e}", color="#e05a5a",
                            in_context=False, min_verbosity=1,
                        ))
                        continue
                else:
                    for d in path_decisions:
                        d["patch"].status = "accepted"
                    patch = path_decisions[0]["patch"]
                    try:
                        prev_content = patch.old_content if patch.kind != "create" else None
                        self.executor.apply_patch(patch)
                        changed_files_for_snapshot[path] = prev_content
                    except Exception as e:
                        for d in path_decisions:
                            d["patch"].status = "failed"
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", f"Failed to apply {d['patch'].path}: {e}", color="#e05a5a",
                            in_context=False, min_verbosity=1,
                        ))
                        continue
                for d in path_decisions:
                    self.executor.stats.record_decision(d["patch"].kind, True)
                self.chat_panel.add_message(ChatMessage.create(
                    "system", f"Applied: {path}", color="#3fa34d", in_context=False, min_verbosity=1,
                ))
                continue

            try:
                disk_content = self.executor.workspace.read_file(path)
            except WorkspaceError:
                disk_content = ""
            if self.draft_store and self.draft_store.has_draft(path):
                accepted_indices = [i for i, d in enumerate(path_decisions) if d["approved"]]
                if not accepted_indices:
                    # Nothing in this batch was approved — discard the draft
                    # untouched. accept_hunks() would still return disk_content
                    # (never None, even with zero accepted regions), and for a
                    # brand-new file disk_content is "" — so write_file() would
                    # have created an empty file for a patch the user rejected.
                    self.draft_store.clear(path)
                    for d in path_decisions:
                        p = d["patch"]
                        p.status = "rejected"
                        self.executor.stats.record_decision(p.kind, False)
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", f"Rejected: {path} (region {p.description})", color="#e05a5a",
                            in_context=False, min_verbosity=1,
                        ))
                    continue
                # accept_hunks() no longer clears the draft itself — same
                # fix as the full-accept branch above.
                new_content = self.draft_store.accept_hunks(path, accepted_indices, disk_content)
                try:
                    if new_content is not None:
                        self.executor.workspace.write_file(path, new_content)
                    self.draft_store.clear(path)
                    changed_files_for_snapshot[path] = disk_content
                    for d in path_decisions:
                        p = d["patch"]
                        if d["approved"]:
                            p.status = "accepted"
                            self.executor.stats.record_decision(p.kind, True)
                            self.chat_panel.add_message(ChatMessage.create(
                                "system", f"Applied: {path} (region {p.description})", color="#3fa34d",
                                in_context=False, min_verbosity=1,
                            ))
                        else:
                            p.status = "rejected"
                            self.executor.stats.record_decision(p.kind, False)
                            self.chat_panel.add_message(ChatMessage.create(
                                "system", f"Rejected: {path} (region {p.description})", color="#e05a5a",
                                in_context=False, min_verbosity=1,
                            ))
                except Exception as e:
                    for d in path_decisions:
                        d["patch"].status = "failed"
                    self.chat_panel.add_message(ChatMessage.create(
                        "system", f"Failed to apply {path}: {e}", color="#e05a5a",
                        in_context=False, min_verbosity=1,
                    ))
                    continue
            else:
                for d in path_decisions:
                    p = d["patch"]
                    if d["approved"]:
                        p.status = "accepted"
                        try:
                            self.executor.apply_patch(p)
                            self.executor.stats.record_decision(p.kind, True)
                            self.chat_panel.add_message(ChatMessage.create(
                                "system", f"Applied: {path}", color="#3fa34d",
                                in_context=False, min_verbosity=1,
                            ))
                        except Exception as e:
                            p.status = "failed"
                            self.chat_panel.add_message(ChatMessage.create(
                                "system", f"Failed to apply {p.path}: {e}", color="#e05a5a",
                                in_context=False, min_verbosity=1,
                            ))
                            continue
                    else:
                        p.status = "rejected"
                        self.executor.stats.record_decision(p.kind, False)
                        self.chat_panel.add_message(ChatMessage.create(
                            "system", f"Rejected: {path}", color="#e05a5a",
                            in_context=False, min_verbosity=1,
                        ))
                if any(d["approved"] for d in path_decisions):
                    changed_files_for_snapshot[path] = None

        decision_summaries = [{"description": f"{d['patch'].kind} {d['patch'].path}", "approved": d["approved"], "path": d["patch"].path} for d in decisions]

        if changed_files_for_snapshot:
            self.snapshot_store.create_snapshot(
                prompt=self._pending_prompt_summary,
                changed_files=changed_files_for_snapshot,
            )
        if self.agent:
            self.agent.notify_patch_decisions(decision_summaries)
        self._update_change_queue_badge()
        self._refresh_file_list()

    # ---------- Window lifecycle ----------

    def closeEvent(self, event):
        self.app_settings.save_geometry(self.saveGeometry())
        if self.draft_store:
            try:
                self.draft_store.flush_to_disk()
            except Exception:
                pass
        self._autosave_current_session()
        try:
            self.runtime.stop()
        except Exception:
            pass
        # The subagent server is a separate llama-server child; the main
        # runtime.stop() above does not cover it — same orphan/port leak.
        try:
            self.subagent_server.stop()
        except Exception:
            pass
        try:
            _close_browser()
        except Exception:
            pass
        super().closeEvent(event)

    def _on_message_edited(self, message: ChatMessage):
        """Synchronize a UI message edit with the agent's raw message history.

        The chat panel and ``agent.messages`` are two different lists: the panel
        has bubbles with no raw counterpart (app notices, and under "Full trace"
        every tool call and tool result), and the raw history has messages with
        no bubble of their own (the tool-call wrapper of every call). Counting
        positions to line them up therefore lands on the wrong message — which
        is what this used to do: an edit to an answer overwrote the neighbouring
        tool result, and once enough bubbles accumulated it overwrote the
        assistant message carrying ``tool_calls``, breaking the tool protocol for
        the next request. The edit then appeared to "do nothing", because the
        text the model was shown was never the text on screen.

        ``core.edit_tracking`` resolves the bubble structurally instead, and
        refuses to write where there is no counterpart.
        """
        if not self.agent:
            return

        panel = self.chat_panel
        try:
            index = next(i for i, m in enumerate(panel.messages) if m is message)
        except StopIteration:
            return

        if not message.in_context:
            # Visible only; nothing in the model's context to keep in sync.
            self._autosave_current_session()
            return

        kinds = [m.kind for m in panel.messages]
        texts = [m.text for m in panel.messages]
        idx, reason = edit_tracking.apply_edit(
            self.agent.messages, kinds, texts, index, message.text)
        if idx is None:
            self.chat_panel.add_message(ChatMessage.create(
                "system", f"Edit kept on screen only — {reason}.",
                color="#d19900", in_context=False, min_verbosity=1,
            ))
            self._autosave_current_session()
            return

        # A "Continue" button enabled/disabled state depends on the text of the
        # last assistant message, so re-evaluate it after an edit.
        self._update_retry_force_buttons()
        self._autosave_current_session()

