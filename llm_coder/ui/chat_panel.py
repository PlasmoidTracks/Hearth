"""
Modular chat panel: one widget per ChatMessage, laid out in a scrollable
QVBoxLayout. Replaces the old single-QTextEdit + raw-HTML-surgery approach.

This revision fixes two issues found in testing:
  * Scroll desync: auto-scroll now waits for the scroll area's actual
    content height to settle (via the scrollbar's rangeChanged signal +
    a deferred QTimer tick) instead of reading bar.maximum() synchronously
    right after a text mutation, which raced Qt's layout pass and produced
    a stale target. Also added "pinned to bottom" tracking so auto-scroll
    politely backs off once the user manually scrolls up to read history.
  * Tool call/result collapse: each message now supports per-instance
    collapse/expand via ChatMessage.is_collapsed, with tool_call/tool_result
    defaulting to a one-line collapsed summary and everything else defaulting
    to expanded (but still collapsible). A message is never collapsed while
    actively streaming.
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QGuiApplication, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QScrollArea,
    QCheckBox, QSizePolicy, QFrame, QPushButton, QMenu,
)

from core.chat_message import ChatMessage, VERBOSITY_LABELS

# Minimum widths applied to a message body label, per wrap mode.
#
# CRITICAL: the value MUST be a small POSITIVE number, not 0. Qt's
# QWidget::minimumWidth() falls back to minimumSizeHint() when the explicitly
# set minimum is 0 — and a QLabel's minimumSizeHint is its full unwrapped line
# width. Setting 0 therefore silently re-imposed the widest line as the floor,
# which made the panel refuse to shrink and showed a horizontal scrollbar
# instead of wrapping. Any positive value overrides the hint.
_WRAP_BODY_MIN_WIDTH = 1       # wrap on: effectively no floor -> text just re-wraps
_NOWRAP_BODY_MIN_WIDTH = 100   # wrap off: small floor so the panel never collapses

# Height of the inline message editor, in pixels. Fixed rather than bounded so
# the Save/Cancel row underneath it is always at the same, reachable offset.
_EDITOR_HEIGHT = 220


class _HFWLabel(QLabel):
    """A wrapping QLabel that reports its height-for-width correctly.

    A plain QLabel breaks height-for-width the moment it is nested inside
    another layout. Its ``heightForWidth()`` is correct, but
    ``minimumSizeHint()`` keeps returning the height of the text as ONE
    UNWRAPPED line, and that bogus minimum is what propagates upward: a
    nested ``QLayout`` sums the children's minimums, and a
    ``QScrollArea(setWidgetResizable(True))`` sizes its content widget to
    ``max(viewport, minimumSizeHint())`` — the MINIMUM, not the sizeHint.

    Result: every long message injected its entire unwrapped height into the
    scroll container's minimum and never gave it back, so the scroll range
    grew far faster than the visible bubbles and never shrank again.
    Measured before the fix: container height 9290px for ~780px of content.

    Overriding BOTH hooks with the real wrapped height is what fixes it.
    ``heightForWidth()`` alone is not enough — it was already correct and
    already advertised via ``sizePolicy().hasHeightForWidth()`` (QLabel sets
    that itself when word wrap is on); the minimum is the value that leaked.
    No size-policy combination (Preferred/Maximum/Ignored/Expanding) can
    work around it, because the lie lives in minimumSizeHint(), not in the
    policy flags.

    ``_wrap_width()`` exists because during a layout pass the label is not
    sized yet, so ``width()`` can be 0; we walk up to the nearest ancestor
    that has a real width and subtract the horizontal chrome that will be
    taken from it.
    """

    #: horizontal chrome subtracted from an ancestor's width to approximate
    #: our own: MessageWidget's outer margins (6+6) plus the ChatPanel
    #: container margins (4+4).
    _WRAP_CHROME_PX = 24

    def _wrap_width(self) -> int:
        w = self.width()
        if w > 1:
            return w
        p = self.parent()
        while p is not None:
            if p.width() > 1:
                return max(1, p.width() - self._WRAP_CHROME_PX)
            p = p.parent()
        return 600

    def sizeHint(self):
        return QSize(QLabel.sizeHint(self).width(),
                     QLabel.heightForWidth(self, self._wrap_width()))

    def minimumSizeHint(self):
        return QSize(_WRAP_BODY_MIN_WIDTH,
                     QLabel.heightForWidth(self, self._wrap_width()))


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _find_panel(widget):
    """Walk up the parent chain to the owning ChatPanel.

    The direct parent of a MessageWidget is the scroll area's inner
    container, not the ChatPanel itself, so a single lookup would miss.
    Duck-typed on purpose: importing ChatPanel here would be circular.
    """
    p = widget.parent()
    while p is not None and not (hasattr(p, "_pinned_to_bottom")
                                 and hasattr(p, "ensureWidgetVisible")):
        p = p.parent()
    return p


def _markdown_to_html_fragment(text: str) -> str:
    """Render markdown text to an HTML fragment suitable for a QLabel.

    Qt's QMarkdown does NOT interpret HTML — it only understands markdown
    syntax. So we must hand it the RAW markdown (unescaped): any literal
    ``<`` / ``>`` in the source are either real markdown (fenced code blocks,
    inline `` `code` ``) or literal text, and QMarkdown is responsible for
    escaping whatever is not markup. Pre-escaping here (the old behaviour)
    turned every ``<`` into ``&lt;`` BEFORE QMarkdown saw it, which:
      * made genuine inline/HTML in the reply render as literal text, and
      * caused the ``<body>`` regex below to miss (Qt's toHtml() often omits
        the closing ``</body>``), sending us down the fallback path that
        returned the already-escaped (i.e. double-escaped) string — the
        "invisible / parsed-as-HTML" symptom.
    """
    md = text or ""
    # NOTE: this temp QTextEdit is used ONLY to run setMarkdown() and extract
    # the resulting HTML fragment. Any stylesheet set here affects only this
    # throwaway widget's own rendering and is NOT embedded in the emitted
    # HTML string — so the <p> margin override that actually matters is
    # applied to the destination QLabel in MessageWidget.update_text().
    temp = QTextEdit()
    temp.setMarkdown(md)
    body_html = temp.toHtml()

    # Qt's toHtml() output is inconsistent across versions: it may wrap the
    # fragment in <html><head><style>…</style></head><body>…</body>, may
    # omit the closing </body>, or may return the bare fragment. Anchor on
    # the <body …> opener (which is always present) and, if a </body> exists,
    # cut there; otherwise use everything after the opener.
    body_start = re.search(r"<body[^>]*>", body_html, re.IGNORECASE)
    if body_start:
        body = body_html[body_start.end():]
    else:
        body = body_html
    body_end = re.search(r"</body>", body, re.IGNORECASE)
    if body_end:
        body = body[:body_end.start()]

    fragment = body
    # Strip any <head>...<style>...</style> artefacts Qt may emit.
    fragment = re.sub(
        r"<style[^>]*>.*?</style>", "", fragment,
        flags=re.DOTALL | re.IGNORECASE,
    )
    fragment = re.sub(
        r"</?(?:html|head|body)[^>]*>", "", fragment,
        flags=re.IGNORECASE,
    )
    fragment = fragment.strip()

    if fragment:
        # Strip the <p> block wrapper Qt's QMarkdown emits around every
        # paragraph, keeping only the inner (inline) content. Qt's rich-text
        # QLabel IGNORES CSS `margin`/`padding` on block elements when it
        # computes its own height — it always applies a fixed block spacing
        # (~one empty line) after each <p>. That is what produced the large
        # empty vertical gap under short, one-line messages (the
        # `p { margin: 0 }` stylesheet override is a no-op for sizing).
        # Removing the <p> tags entirely removes that block spacing, so a
        # single line of markdown hugs its text.
        fragment = re.sub(
            r"<p[^>]*>(.*?)</p>",
            lambda m: m.group(1),
            fragment,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Unwrapping adjacent <p> blocks runs their content together; rejoin
        # with a line break so multi-paragraph messages still separate. A
        # <br> is an inline element, so it does NOT reintroduce block spacing.
        fragment = re.sub(r"\s*<br\s*/?>\s*", "<br>", fragment)
        fragment = fragment.strip()
        if fragment:
            return fragment

    # Fallback: QMarkdown produced nothing usable. Show the text as plain
    # escaped text (NOT re-escaping anything that is already escaped) with
    # newlines preserved as <br>.
    return _escape(md).replace("\n", "<br>")


class MessageWidget(QFrame):
    """A single chat bubble bound 1:1 to a ChatMessage."""
    context_toggled = Signal(object, bool)  # (ChatMessage, in_context)
    collapse_toggled = Signal()             # fired on any expand/collapse (for scroll-follow)
    rewind_requested = Signal(int)          # message.id to rewind to
    duplicate_requested = Signal(int)       # message.id to branch from
    duplicate_message_requested = Signal(int)  # message.id to copy in place
    text_changed = Signal(object)           # fired when message text is edited (ChatMessage)

    def __init__(self, message: ChatMessage, render_mode: str = "markdown",
                 force_wrap: bool = True, parent=None):
        super().__init__(parent)
        self.message = message
        self.render_mode = render_mode
        self.setFrameShape(QFrame.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 3, 6, 3)
        outer.setSpacing(2)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)

        self.collapse_btn: Optional[QPushButton] = None
        if message.show_collapse_toggle:
            self.collapse_btn = QPushButton()
            self.collapse_btn.setFixedWidth(18)
            self.collapse_btn.setFlat(True)
            self.collapse_btn.setStyleSheet(
                "QPushButton { border:none; color:#888; font-size:11px; } "
                "QPushButton:hover { color:#ccc; }"
            )
            self.collapse_btn.clicked.connect(self._on_collapse_clicked)
            header_row.addWidget(self.collapse_btn)

        self.header_label = QLabel()
        header_row.addWidget(self.header_label)
        header_row.addStretch()

        self.context_checkbox: Optional[QCheckBox] = None
        if message.editable_context:
            self.context_checkbox = QCheckBox("in context")
            self.context_checkbox.setChecked(message.in_context)
            self.context_checkbox.setStyleSheet("color:#8a8a8a; font-size:10px;")
            self.context_checkbox.toggled.connect(self._on_context_toggled)
            header_row.addWidget(self.context_checkbox)
        outer.addLayout(header_row)

        self.body = _HFWLabel()
        self.body.setWordWrap(force_wrap)
        # Small POSITIVE minimum (see constants above): a 0 would fall back to
        # the label's minimumSizeHint (the full line width) and re-impose a big
        # floor. With wrapping on this lets the panel be squeezed narrow and
        # the text re-wraps; with wrapping off it keeps a modest floor.
        self.body.setMinimumWidth(_WRAP_BODY_MIN_WIDTH if force_wrap else _NOWRAP_BODY_MIN_WIDTH)
        self.body.setTextFormat(Qt.RichText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.body.setCursor(Qt.PointingHandCursor if message.show_collapse_toggle else Qt.ArrowCursor)
        outer.addWidget(self.body)

        # Edit mode widgets (initially hidden).
        #
        # FIXED height, deliberately: a QTextEdit's height-for-width sizeHint is
        # about ten lines, so with only a maximum set, editing a long message
        # grew the bubble all the way up to that maximum and pushed the
        # Save/Cancel row to the very bottom of it — you had to scroll the chat
        # to reach the buttons, and the field itself scrolled internally at the
        # same time. Pinning the height means the bubble stops growing the moment
        # it turns into an editor and the buttons always sit the same short
        # distance below it, no matter how much text is in there.
        self.editor = QTextEdit()
        self.editor.setPlainText(message.text or "")
        self.editor.setPlaceholderText("Edit message...")
        self.editor.setVisible(False)
        self.editor.setFixedHeight(_EDITOR_HEIGHT)
        self.editor.setStyleSheet("background-color: #2b2b2b; color: #eee; border: 1px solid #555;")
        outer.addWidget(self.editor)

        self.edit_btns = QWidget()
        self.edit_btns.setVisible(False)
        btn_layout = QHBoxLayout(self.edit_btns)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(5)

        self.save_btn = QPushButton("Save")
        self.save_btn.setFixedWidth(60)
        self.save_btn.clicked.connect(self._on_save_edit)
        btn_layout.addWidget(self.save_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFixedWidth(60)
        self.cancel_btn.clicked.connect(self._on_cancel_edit)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch()
        outer.addWidget(self.edit_btns)

        self._refresh_header()
        self.update_text()

    def mousePressEvent(self, event):
        # Left-click anywhere in the body toggles collapse, for
        # tool_call/tool_result bubbles where the one-line summary IS the
        # click target — not just the small arrow button.
        if (event.button() == Qt.LeftButton
                and self.message.show_collapse_toggle
                and not self.message.streaming):
            self._on_collapse_clicked()
        super().mousePressEvent(event)

    def _on_collapse_clicked(self):
        self.message.toggle_collapsed()
        self.update_text()
        self._refresh_header()
        self.collapse_toggled.emit()

    def _on_context_toggled(self, checked: bool):
        self.message.in_context = checked
        self.context_toggled.emit(self.message, checked)
        self._refresh_header()

    def _refresh_header(self):
        label = self.message.role_label or ""
        color = self.message.color or "#8a8a8a"
        ctx_badge = ""
        if self.message.editable_context:
            ctx_badge = (
                ' <span style="color:#5aa85a; font-size:10px;">[in context]</span>'
                if self.message.in_context else
                ' <span style="color:#666; font-size:10px;">[not in context]</span>'
            )
        elif self.message.kind in ("tool_call", "tool_result", "tool_error", "user", "assistant"):
            ctx_badge = ' <span style="color:#5aa85a; font-size:10px;">[in context]</span>'
        streaming_badge = ' <span style="color:#e0c95a; font-size:10px;">● live</span>' if self.message.streaming else ""
        self.header_label.setText(
            f'<span style="color:{color}; font-weight:bold;">{label}</span>{ctx_badge}{streaming_badge}'
        )
        if self.collapse_btn is not None:
            self.collapse_btn.setText("▸" if self.message.is_collapsed else "▾")
            self.collapse_btn.setToolTip("Expand" if self.message.is_collapsed else "Collapse")

    def update_text(self):
        """Repaint this widget's body from the message's current text.
        Collapsed (finalized, non-streaming) messages show a one-line
        summary instead of full text."""
        if self.message.is_collapsed:
            self.body.setStyleSheet("color:#8a8a8a; font-style:italic;")
            self.body.setText(_escape(self.message.summary_text()))
            self._sync_wrap_width()
            return

        text = self.message.text or ""
        if self.message.is_mono:
            html = _escape(text).replace("\n", "<br>")
            self.body.setStyleSheet(
                f"font-family:monospace; font-size:11px; color:{self.message.color};"
                f"border-left:2px solid {self.message.color}; padding-left:6px;"
            )
            self.body.setText(html)
            self._sync_wrap_width()
            return

        if self.message.streaming:
            self.body.setStyleSheet("")
            self.body.setText(_escape(text).replace("\n", "<br>"))
        else:
            if self.render_mode == "markdown":
                # _markdown_to_html_fragment() strips the <p> block wrappers
                # Qt's QMarkdown emits, because Qt's rich-text QLabel ignores
                # CSS margins on block elements and would otherwise add a
                # large empty vertical gap under short (one-line) messages.
                # The p-margin rule below is a harmless belt-and-suspenders
                # fallback in case any <p> ever slips through.
                self.body.setStyleSheet("p { margin-top: 0; margin-bottom: 0; }")
                self.body.setText(_markdown_to_html_fragment(text))
            elif self.render_mode == "monospace":
                self.body.setText(f"<pre>{_escape(text)}</pre>")
            else:
                self.body.setText(_escape(text).replace("\n", "<br>"))
        self._sync_wrap_width()

    def _sync_wrap_width(self):
        """Re-resolve the body's wrapped height against its CURRENT width.

        ``_HFWLabel`` reports its height by calling ``heightForWidth()`` at
        whatever width the label has at the time it is asked. During a layout
        pass that width is not final yet, so it falls back to an ancestor's
        width — an approximation. Once the label has actually been laid out we
        know its real width, so we re-resolve and let Qt cache the correct
        value.

        Guarded on a width change: ``updateGeometry()`` posts a LayoutRequest,
        and calling it unconditionally from inside a layout pass would keep
        re-posting one and livelock the event loop.
        """
        body = self.body
        w = body.width()
        if w <= 1 or w == getattr(self, "_resolved_body_width", -1):
            return
        self._resolved_body_width = w
        body.updateGeometry()

    def set_force_wrap(self, enabled: bool):
        """Toggle word-wrapping on this message's body.

        When wrapping is ON, the body gets a tiny POSITIVE minimum width so
        the label's sizeHint (one full line of text) no longer acts as a
        floor: the panel can be squeezed narrow and the text re-wraps into
        more lines. (A minimum of 0 would not work — Qt falls back to
        minimumSizeHint in that case, which is the full line width.)

        When wrapping is OFF, long lines are not wrapped; the body keeps a
        small floor so the panel never collapses to nothing.
        """
        self.body.setWordWrap(enabled)
        self.body.setMinimumWidth(_WRAP_BODY_MIN_WIDTH if enabled else _NOWRAP_BODY_MIN_WIDTH)

    def refresh_after_finalize(self):
        """Called once when streaming ends: re-render text AND re-evaluate
        collapse state, since a message that was too short to need a
        collapse toggle while streaming might need one now that it's full
        length (e.g. tool_call args that grew past the threshold)."""
        needs_toggle = self.message.show_collapse_toggle
        if needs_toggle and self.collapse_btn is None:
            self.collapse_btn = QPushButton()
            self.collapse_btn.setFixedWidth(18)
            self.collapse_btn.setFlat(True)
            self.collapse_btn.setStyleSheet(
                "QPushButton { border:none; color:#888; font-size:11px; } "
                "QPushButton:hover { color:#ccc; }"
            )
            self.collapse_btn.clicked.connect(self._on_collapse_clicked)
            layout = self.layout().itemAt(0).layout()
            layout.insertWidget(0, self.collapse_btn)
            self.body.setCursor(Qt.PointingHandCursor)
        self._refresh_header()
        self.update_text()

    def set_visible_for_verbosity(self, level: int):
        self.setVisible(self.message.visible_at(level))

    def contextMenuEvent(self, event):
        """Show a context menu with Copy, Edit, Collapse/Expand, and Rewind options when right-clicking on a message."""
        menu = QMenu(self)
        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(self._on_copy)
        menu.addAction(copy_action)
        
        # Only allow editing of finalized messages
        if not self.message.streaming:
            edit_action = QAction("Edit", self)
            edit_action.triggered.connect(self.enter_edit_mode)
            menu.addAction(edit_action)
            menu.addSeparator()

        collapse_action = QAction("Toggle collapse", self)
        collapse_action.triggered.connect(self._on_collapse_clicked)
        menu.addAction(collapse_action)
        duplicate_msg_action = QAction("Duplicate message", self)
        duplicate_msg_action.setToolTip(
            "Insert a copy of this message right below the original, so you can "
            "edit it and continue down a different path."
        )
        duplicate_msg_action.triggered.connect(
            lambda: self.duplicate_message_requested.emit(self.message.id))
        menu.addAction(duplicate_msg_action)
        menu.addSeparator()
        rewind_action = QAction("Rewind to here", self)
        rewind_action.setToolTip(
            "Cut the conversation off at this message. Everything after it is "
            "removed from this chat."
        )
        rewind_action.triggered.connect(lambda: self.rewind_requested.emit(self.message.id))
        menu.addAction(rewind_action)
        # Kept next to Rewind on purpose: same target message, opposite effect
        # on the original. Branch copies everything up to here into a NEW chat
        # and leaves this one exactly as it is, so the two ways to take the
        # conversation down a different road sit side by side and the
        # destructive one is no longer the only one on offer.
        branch_action = QAction("Branch chat from here", self)
        branch_action.setToolTip(
            "Duplicate this chat up to this message into a new session, and keep "
            "working there. This chat is left untouched, so you can explore an "
            "alternate continuation without losing the one you have.\n\n"
            "Branching from a user message keeps your prompt and drops the "
            "answer, so the agent replies again from the same point."
        )
        branch_action.triggered.connect(lambda: self.duplicate_requested.emit(self.message.id))
        menu.addAction(branch_action)
        menu.exec(event.globalPos())

    def _find_panel(widget):
        """Walk up the parent chain to the owning ChatPanel.

        The direct parent of a MessageWidget is the scroll area's inner
        container, not the ChatPanel itself, so a single lookup would miss.
        Duck-typed on purpose: importing ChatPanel here would be circular.
        """
        p = widget.parent()
        while p is not None and not (hasattr(p, "_pinned_to_bottom")
                                     and hasattr(p, "ensureWidgetVisible")):
            p = p.parent()
        return p

    def enter_edit_mode(self):
        """Switch from label view to text editor view.

        The body label is hidden, not left in place above the editor: two tall
        widgets stacked in one bubble is what made the panel jump and pushed the
        buttons out of view. The editor is a fixed height, so the bubble's size
        while editing is constant and Save/Cancel stay right below it.
        """
        self.body.setVisible(False)
        self.editor.setVisible(True)
        self.edit_btns.setVisible(True)
        self.editor.setPlainText(self.message.text or "")
        self.editor.setFocus()
        # The panel auto-scrolls to the bottom on content changes, which would
        # fight the user who scrolled up to edit an older bubble. Stop following
        # the bottom and just bring this bubble into view.
        panel = _find_panel(self)
        if panel is not None:
            panel._pinned_to_bottom = False
            panel.ensureWidgetVisible(self.editor)

    def _on_save_edit(self):
        """Save the edited text back to the message and return to label view."""
        new_text = self.editor.toPlainText()
        if new_text != self.message.text:
            self.message.text = new_text
            self.text_changed.emit(self.message)
        
        self._on_cancel_edit()

    def _on_cancel_edit(self):
        """Cancel editing and return to label view."""
        self.body.setVisible(True)
        self.editor.setVisible(False)
        self.edit_btns.setVisible(False)
        self.update_text()

    def _on_copy(self):
        """Copy the message text to the clipboard."""
        text = self.message.text or ""
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(text)


class ChatPanel(QScrollArea):
    """Scrollable container of MessageWidgets, one per ChatMessage."""
    context_changed = Signal()
    rewind_requested = Signal(int)  # message.id to rewind to
    duplicate_requested = Signal(int)  # message.id to branch a new chat from
    duplicate_message_requested = Signal(int)  # message.id to copy in place
    message_edited = Signal(object)  # fired when a ChatMessage is edited (ChatMessage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._verbosity_level = VERBOSITY_LABELS.get("Full trace", 2)
        self._render_mode = "markdown"
        self._force_wrap = True
        self._pinned_to_bottom = True
        self._scroll_pending = False
        self._auto_scroll = False

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(0)
        self._layout.addStretch()
        self.setWidget(container)

        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_scroll_value_changed)

        self.messages: list[ChatMessage] = []
        self._widgets: dict[int, MessageWidget] = {}

    # ---------- configuration ----------

    def set_verbosity(self, level_or_name):
        level = VERBOSITY_LABELS.get(level_or_name, level_or_name) if isinstance(level_or_name, str) else level_or_name
        self._verbosity_level = level
        for w in self._widgets.values():
            w.set_visible_for_verbosity(level)

    def set_render_mode(self, mode: str):
        self._render_mode = mode
        for w in self._widgets.values():
            w.render_mode = mode
            if not w.message.streaming:
                w.update_text()

    def set_force_wrap(self, enabled: bool):
        """Enable/disable force word-wrapping for all messages (new and
        existing).

        On:  text wraps to the panel width; the panel has no meaningful
             minimum width — it can be squeezed narrow and the text just
             re-wraps into more lines.
        Off: long lines are not wrapped; they extend the panel width and a
             horizontal scrollbar appears instead (with a small minimum
             width floor so the panel never collapses to nothing).
        """
        self._force_wrap = bool(enabled)
        for w in self._widgets.values():
            w.set_force_wrap(enabled)

    def set_auto_scroll(self, enabled: bool):
        """Enable/disable following the conversation to the bottom.

        On:  every new message and every streamed chunk pulls the viewport to
             the bottom — but only while the view is still pinned there, so
             scrolling up to read history interrupts the follow until the user
             returns to the bottom (see _on_scroll_value_changed).
        Off: the panel never moves the scrollbar on its own.

        Turning it on re-asserts the pin and snaps to the bottom once, so the
        toggle takes effect visibly immediately rather than only on the next
        message.
        """
        self._auto_scroll = bool(enabled)
        if self._auto_scroll:
            self._request_scroll_to_bottom_if_pinned(force=True)

    def auto_scroll(self) -> bool:
        return self._auto_scroll

    # ---------- message lifecycle ----------

    def add_message(self, message: ChatMessage) -> MessageWidget:
        self.messages.append(message)
        widget = MessageWidget(message, render_mode=self._render_mode,
                               force_wrap=self._force_wrap)
        widget.context_toggled.connect(lambda *_: self.context_changed.emit())
        widget.collapse_toggled.connect(self._request_scroll_to_bottom_if_pinned)
        widget.rewind_requested.connect(self._on_rewind_requested)
        widget.duplicate_requested.connect(self._on_duplicate_requested)
        widget.duplicate_message_requested.connect(self._on_duplicate_message_requested)
        widget.text_changed.connect(lambda msg: self.message_edited.emit(msg))
        widget.set_visible_for_verbosity(self._verbosity_level)
        insert_at = self._layout.count() - 1
        self._layout.insertWidget(insert_at, widget)
        self._widgets[message.id] = widget
        # NOT force=True. A new message is the strongest "show me the newest
        # thing" event, but force overrides the _pinned_to_bottom gate — which
        # meant that with auto-scroll enabled a new message yanked the viewport
        # down even while the user was deliberately scrolled up reading
        # history. That is precisely the behaviour that got this whole feature
        # switched off originally, so following now obeys the pin everywhere:
        # add_message follows if (and only if) the user was already at the
        # bottom.
        self._request_scroll_to_bottom_if_pinned()
        return widget

    def append_to_message(self, message: ChatMessage, piece: str):
        message.append(piece)
        widget = self._widgets.get(message.id)
        if widget is not None:
            widget.update_text()
            self._request_scroll_to_bottom_if_pinned()

    def finalize_message(self, message: ChatMessage):
        """Mark a streaming message as complete and trigger its one-time
        markdown re-render + collapse-state re-evaluation."""
        message.streaming = False
        widget = self._widgets.get(message.id)
        if widget is not None:
            widget.refresh_after_finalize()
            self._request_scroll_to_bottom_if_pinned()

    def refresh_message(self, message: ChatMessage):
        """Re-render an existing message from its current state, without
        changing that state.

        The inverse of finalize_message(): used when a message goes back INTO
        streaming mode (e.g. "Continue" resuming an existing assistant bubble),
        so the header drops the stale "● live" flag logic and the body switches
        to the plain streaming renderer instead of the markdown one.
        """
        widget = self._widgets.get(message.id)
        if widget is None:
            return
        widget._refresh_header()
        widget.update_text()
        # Same rule as add_message(): resume-following only if the user is
        # already at the bottom.
        self._request_scroll_to_bottom_if_pinned()

    def clear(self):
        for w in list(self._widgets.values()):
            w.setParent(None)
        self._widgets.clear()
        self.messages.clear()
        self._pinned_to_bottom = True

    def rebuild_from(self, messages: list[ChatMessage]):
        self.clear()
        for m in messages:
            self.add_message(m)
        # add_message() no longer forces the scroll, so a bulk rebuild would
        # otherwise land the user at the TOP of the restored conversation.
        # Treat "load a session" as an explicit jump to the newest message —
        # but only when the user asked for following in the first place.
        if self._auto_scroll:
            self._request_scroll_to_bottom_if_pinned(force=True)

    def rewind_to(self, message_id: int):
        """Remove all messages after the given message_id.

        Keeps the message with message_id and everything before it.
        Destroys widgets for removed messages and cleans up internal state.
        """
        # Find the index of the message to keep
        try:
            keep_idx = next(i for i, m in enumerate(self.messages) if m.id == message_id)
        except StopIteration:
            return  # message_id not found, nothing to do

        messages_to_remove = self.messages[keep_idx + 1:]
        self.messages = self.messages[:keep_idx + 1]

        # Destroy widgets for removed messages
        for msg in messages_to_remove:
            widget = self._widgets.pop(msg.id, None)
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def remove_last_message(self) -> Optional[ChatMessage]:
        """Remove and destroy the most recent message (if any).

        Symmetric to rewind_to(): pops the tail of ``self.messages``, destroys
        its MessageWidget, and returns the removed ChatMessage so the caller
        can inspect it (e.g. to decide what to do with it). Returns None when
        the panel is already empty. Used by the UI's "Retry" flow to discard
        the last agent response before regenerating a fresh one.
        """
        if not self.messages:
            return None
        message = self.messages.pop()
        widget = self._widgets.pop(message.id, None)
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        self._request_scroll_to_bottom_if_pinned()
        return message

    def _on_rewind_requested(self, message_id: int):
        self.rewind_requested.emit(message_id)

    def _on_duplicate_requested(self, message_id: int):
        self.duplicate_requested.emit(message_id)

    def _on_duplicate_message_requested(self, message_id: int):
        self.duplicate_message_requested.emit(message_id)

    def insert_message(self, message: ChatMessage, index: int) -> MessageWidget:
        """Insert a bubble at a specific position in the conversation.

        ``add_message`` can only append. This is the other half of "duplicate a
        message": a copy has to land right after the bubble it was copied from,
        not at the very end of the log. Indices are clamped, so an out-of-range
        index degrades into an append rather than throwing.
        """
        index = max(0, min(index, len(self.messages)))
        self.messages.insert(index, message)
        widget = MessageWidget(message, render_mode=self._render_mode,
                               force_wrap=self._force_wrap)
        widget.context_toggled.connect(lambda *_: self.context_changed.emit())
        widget.collapse_toggled.connect(self._request_scroll_to_bottom_if_pinned)
        widget.rewind_requested.connect(self._on_rewind_requested)
        widget.duplicate_requested.connect(self._on_duplicate_requested)
        widget.duplicate_message_requested.connect(self._on_duplicate_message_requested)
        widget.text_changed.connect(lambda msg: self.message_edited.emit(msg))
        widget.set_visible_for_verbosity(self._verbosity_level)
        # The trailing addStretch() occupies the last slot, so bubbles live at
        # 0..count-1 and inserting at `index` puts the widget exactly there.
        self._layout.insertWidget(index, widget)
        self._widgets[message.id] = widget
        self._request_scroll_to_bottom_if_pinned()
        return widget

    def message_index(self, message_id: int) -> Optional[int]:
        """Position of a bubble by id, or None when the panel has no such bubble."""
        for i, m in enumerate(self.messages):
            if m.id == message_id:
                return i
        return None

    # ---------- context export ----------

    def context_snapshot(self) -> list[ChatMessage]:
        return [m for m in self.messages if m.in_context]

    def to_persist_dicts(self) -> list[dict]:
        return [m.to_dict() for m in self.messages]

    @staticmethod
    def messages_from_persist_dicts(dicts: list[dict]) -> list[ChatMessage]:
        return [ChatMessage.from_dict(d) for d in dicts]

    # ---------- scroll handling ----------
    #
    # AUTO-SCROLL IS USER-TOGGLED (default: off).
    #
    # When the toggle is off, the panel never moves the scrollbar on its own:
    # new messages, streamed text, collapse toggles and re-renders all leave
    # the viewport exactly where the user put it. When it is on, the panel
    # follows the conversation to the bottom — but only while the view is
    # pinned there, so scrolling up to read history still interrupts.
    #
    # The switch lives on the instance (_auto_scroll, set via
    # set_auto_scroll()) and is wired to Settings → Agent Behavior →
    # "Auto-scroll to bottom". _request_scroll_to_bottom_if_pinned() is the
    # single choke point every mutation path funnels through, so the whole
    # feature turns on and off in one place.
    #
    # History, for whoever ends up here next:
    #   * The original auto-scroll driver was commented out at the
    #     QTimer.singleShot(0, ...) line. What was NOT obvious at the time is
    #     that the surrounding code set _scroll_pending = True without ever
    #     clearing it, so the _on_range_changed() path latched itself off on
    #     its very first call. Re-enabling that timer restored following, but
    #     it also made add_message() (force=True) yank the viewport to the
    #     bottom even when the user had deliberately scrolled up to read
    #     history — which is the behaviour that was explicitly turned off
    #     here, and why following stays gated on _pinned_to_bottom.
    #   * The "scrollbar grows much faster than the bubbles" bug was never a
    #     scroll-timing problem at all and is fixed in _HFWLabel. Don't
    #     re-derive a scroll-side fix for it.

    def _request_scroll_to_bottom_if_pinned(self, force: bool = False):
        """Schedule a scroll to the bottom, unless disabled or unpinned.

        ``force`` overrides the _pinned_to_bottom gate — i.e. "snap to the
        bottom no matter where the user is reading". Only the user's own
        explicit actions (the auto-scroll toggle, rebuild_from) may use it;
        message-arrival paths must NOT, or they fight the user's scroll-up.
        """
        if force:
            self._pinned_to_bottom = True
        if not self._auto_scroll:
            return
        if not self._pinned_to_bottom:
            return
        if self._scroll_pending:
            return
        self._scroll_pending = True
        QTimer.singleShot(0, self._do_scroll_to_bottom)

    def _do_scroll_to_bottom(self):
        self._scroll_pending = False
        self._drain_layout_cascade()
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        # Belt-and-suspenders: geometry changes can still be settling one
        # frame later (e.g. the splitter/viewport itself adjusting once the
        # scrollbar's own presence changes viewport width). Re-check shortly
        # after via the normal event loop, not a synchronous drain, so we
        # don't fight the user if they scrolled away in the meantime.
        QTimer.singleShot(50, self._settle_scroll)

    def _drain_layout_cascade(self):
        """Re-sync the container to its sizeHint before trusting the
        scrollbar's maximum().

        NOTE: an earlier comment here claimed this was the "root cause of the
        scroll-desync bug". That diagnosis was wrong and is kept only as a
        warning. The real cause was that a wrapped QLabel's minimumSizeHint()
        reports the height of its text as ONE UNWRAPPED line, and
        QScrollArea(setWidgetResizable(True)) sizes its content from
        max(viewport, minimumSizeHint()) — the MINIMUM, not the sizeHint. So
        the container was being stretched to satisfy a minimum that no visible
        widget needed. That is fixed in _HFWLabel, which overrides
        sizeHint()/minimumSizeHint() to return the real height-for-width.

        No size-policy change could work around it (Preferred, Maximum,
        Ignored and Expanding were all measured), because the bad value comes
        from minimumSizeHint() rather than from the policy flags.

        This method is now just a cheap safety net around the deferred scroll.
        """
        container = self.widget()
        if container is None:
            return
        if container.layout() is not None:
            container.layout().activate()
        target_height = container.sizeHint().height()
        if container.height() != target_height:
            container.resize(container.width(), target_height)

    def resizeEvent(self, event):
        """Re-resolve every body label's wrapped height when our width changes.

        Wrapped text height is a function of width, so dragging the chat
        splitter (or resizing the window) changes the height every bubble
        needs. Qt's cached height-for-width does not reliably recompute for a
        label whose own width is pinned small and which wraps to its
        allocation, so we re-assert it explicitly.

        Guarded on an actual width change — the scrollbar appearing/disappearing
        shifts the viewport width by ~15px, which re-triggers this once, which
        is correct (the wrap width really did change).
        """
        super().resizeEvent(event)
        w = self.viewport().width()
        if w == getattr(self, "_resolved_viewport_width", -1):
            return
        self._resolved_viewport_width = w
        for widget in self._widgets.values():
            widget._resolved_body_width = -1
            widget._sync_wrap_width()

    def _settle_scroll(self):
        if not self._pinned_to_bottom:
            return
        self._drain_layout_cascade()
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_range_changed(self, _min: int, _max: int):
        """Content height changed. Follow the bottom if (and only if) the
        toggle is on and we're pinned. Note this deliberately does NOT go
        through the force path — a content-height change is not a "new message"
        event, and _scroll_pending is cleared in _do_scroll_to_bottom() so this
        cannot latch itself off.
        """
        if self._auto_scroll and self._pinned_to_bottom:
            self._request_scroll_to_bottom_if_pinned()

    def _on_scroll_value_changed(self, value: int):
        bar = self.verticalScrollBar()
        # Consider the user "pinned to bottom" if they're within a small
        # threshold of the max — avoids fighting them the instant they
        # nudge the wheel by one notch while still basically at the bottom.
        threshold = 24
        self._pinned_to_bottom = (bar.maximum() - value) <= threshold
