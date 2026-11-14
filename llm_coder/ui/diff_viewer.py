"""
Diff viewer / patch approval widget. Shows a unified-diff-like rendering of a
PendingPatch and lets the user Accept or Reject it. Decided patches remain
visible with a clear status badge instead of disappearing. Cards are
collapsible; newest patches appear at the top; accepted/rejected patches
are archived to a collapsed section at the bottom.
"""
import difflib
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton, QFrame
)
from PySide6.QtGui import QFont
from PySide6.QtCore import Signal

from core.tools import PendingPatch

STATUS_COLORS = {
    "pending": ("#3a3a2a", "#e0c95a", "PENDING REVIEW"),
    "accepted": ("#0f2a17", "#3fa34d", "ACCEPTED"),
    "rejected": ("#2a1010", "#e05a5a", "REJECTED"),
    "failed": ("#2a1010", "#e05a5a", "FAILED"),
    "superseded": ("#20202a", "#8a8a8a", "SUPERSEDED"),
}

COLLAPSED_HEIGHT = 0
EXPANDED_HEIGHT = 260


def make_diff_html(old_text: str, new_text: str) -> str:
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
    html_lines = []
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if line.startswith("+"):
            html_lines.append(f'<div style="color:#3fa34d;">{escaped}</div>')
        elif line.startswith("-"):
            html_lines.append(f'<div style="color:#e05a5a;">{escaped}</div>')
        elif line.startswith("@@"):
            html_lines.append(f'<div style="color:#5591c7;">{escaped}</div>')
        else:
            html_lines.append(f'<div>{escaped}</div>')
    if not html_lines:
        return "(no textual differences)"
    return "".join(html_lines)


import time

class PatchCard(QFrame):
    decided = Signal(object, bool)  # (patch, approved)
    collapse_changed = Signal()

    def __init__(self, patch: PendingPatch, parent=None):
        super().__init__(parent)
        self.patch = patch
        self.created_at = time.strftime("%H:%M:%S")
        self.decided_at = None
        self.expanded = (patch.status == "pending")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid #3a3a3a; border-radius: 6px; margin: 4px; }")

        self.layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedWidth(24)
        self.toggle_btn.clicked.connect(self._toggle_collapse)
        header_row.addWidget(self.toggle_btn)
        self.header_label = QLabel()
        header_row.addWidget(self.header_label)
        header_row.addStretch()
        self.layout.addLayout(header_row)

        self.diff_view = QTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setFont(QFont("Monospace", 9))
        if patch.kind == "delete":
            self.diff_view.setHtml(f"File will be deleted: <b>{patch.path}</b>")
        else:
            self.diff_view.setHtml(make_diff_html(patch.old_content, patch.new_content))
        self.layout.addWidget(self.diff_view)

        self.btn_row = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setStyleSheet("QPushButton { background:#256b30; color:white; padding:4px 12px; }")
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setStyleSheet("QPushButton { background:#7a2626; color:white; padding:4px 12px; }")
        self.accept_btn.clicked.connect(lambda: self._decide(True))
        self.reject_btn.clicked.connect(lambda: self._decide(False))
        self.btn_row.addStretch()
        self.btn_row.addWidget(self.reject_btn)
        self.btn_row.addWidget(self.accept_btn)
        self.layout.addLayout(self.btn_row)

        self._refresh_header()
        self._apply_collapse_state()

    def _toggle_collapse(self):
        self.expanded = not self.expanded
        self._apply_collapse_state()
        self.collapse_changed.emit()

    def _apply_collapse_state(self):
        self.toggle_btn.setText("▾" if self.expanded else "▸")
        self.diff_view.setVisible(self.expanded)
        if self.expanded:
            self.diff_view.setMinimumHeight(EXPANDED_HEIGHT)
        else:
            self.diff_view.setMinimumHeight(COLLAPSED_HEIGHT)

    def _refresh_header(self):
        kind_label = {"create": "NEW FILE", "modify": "MODIFY", "delete": "DELETE"}.get(
            self.patch.kind, self.patch.kind.upper()
        )
        bg, fg, badge_text = STATUS_COLORS.get(self.patch.status, STATUS_COLORS["pending"])
        time_str = self.decided_at if self.decided_at else self.created_at
        time_label = f"decided {time_str}" if self.decided_at else f"proposed {time_str}"
        self.header_label.setText(
            f"<b>{kind_label}</b> {self.patch.path} "
            f"<span style='background:{bg}; color:{fg}; padding:2px 6px; border-radius:4px;'>{badge_text}</span> "
            f"<span style='color:#8a8a8a; font-size:11px;'>({time_label})</span>"
        )
        is_pending = self.patch.status == "pending"
        self.accept_btn.setVisible(is_pending)
        self.reject_btn.setVisible(is_pending)

    def _decide(self, approved: bool):
        self.patch.status = "accepted" if approved else "rejected"
        self.decided_at = time.strftime("%H:%M:%S")
        self.expanded = False
        self._refresh_header()
        self._apply_collapse_state()
        self.decided.emit(self.patch, approved)


class PatchReviewPanel(QWidget):
    """
    Persistent change queue: newest pending patches appear at the top.
    Decided (accepted/rejected) patches are moved into a collapsed
    "Archive" section at the bottom rather than staying interleaved.
    """

    all_decided = Signal(list)  # list of {"patch":..., "approved": bool}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)

        self.pending_container = QVBoxLayout()
        self.layout.addLayout(self.pending_container)

        archive_header_row = QHBoxLayout()
        self.archive_toggle_btn = QPushButton("▸ Archive (0)")
        self.archive_toggle_btn.clicked.connect(self._toggle_archive)
        archive_header_row.addWidget(self.archive_toggle_btn)
        archive_header_row.addStretch()
        self.layout.addLayout(archive_header_row)

        self.archive_container = QVBoxLayout()
        self.layout.addLayout(self.archive_container)
        self.archive_expanded = False

        self.layout.addStretch()

        self.cards = []          # all cards, newest-pending-first order maintained separately
        self.decisions_by_path = {}   # path -> list of not-yet-emitted decisions for that path
        self.expected_by_path = {}    # path -> decisions needed before emitting for that path
        self._refresh_archive_visibility()

    def add_patches(self, patches):
        """Insert new pending patches at the top of the pending section.

        Any still-pending card touching the same file path as an incoming
        patch is marked "superseded" and archived first, since regenerating
        draft patches for a file makes the previous unreviewed batch for
        that file stale (it was diffed against an older compounded state).
        Without this, duplicate/overlapping cards for the same lines would
        stack up every time the draft store regenerates patches.
        """
        incoming_paths = {p.path for p in patches}
        for card in list(self.cards):
            if card.patch.status == "pending" and card.patch.path in incoming_paths:
                card.patch.status = "superseded"
                card.decided_at = time.strftime("%H:%M:%S")
                card.expanded = False
                card._refresh_header()
                card._apply_collapse_state()
                self.pending_container.removeWidget(card)
                self.archive_container.insertWidget(0, card)
                card.setVisible(self.archive_expanded)

        # expected = decisions already made in the current still-open batch
        # (if any) + cards still genuinely pending after the supersede pass
        # above. Must include the existing decision count, not just pending
        # cards — otherwise a batch that arrives while an earlier batch is
        # only partially decided under-counts `expected` and triggers
        # all_decided one decision too early, stranding a card as pending.
        for patch in reversed(patches):
            card = PatchCard(patch)
            card.decided.connect(self._on_decided)
            if patch.status == "pending":
                self.pending_container.insertWidget(0, card)
            else:
                self.archive_container.insertWidget(0, card)
                card.setVisible(self.archive_expanded)
            self.cards.append(card)
        pending_paths = {c.patch.path for c in self.cards if c.patch.status == "pending"}
        for path in pending_paths:
            still_pending_for_path = sum(
                1 for c in self.cards if c.patch.status == "pending" and c.patch.path == path
            )
            already_decided = len(self.decisions_by_path.get(path, []))
            self.expected_by_path[path] = already_decided + still_pending_for_path
        self._refresh_archive_visibility()

    def clear_all(self):
        for c in self.cards:
            c.setParent(None)
        self.cards = []
        self.decisions_by_path = {}
        self.expected_by_path = {}
        self._refresh_archive_visibility()

    def _toggle_archive(self):
        self.archive_expanded = not self.archive_expanded
        self._refresh_archive_visibility()

    def _refresh_archive_visibility(self):
        archived_count = sum(1 for c in self.cards if c.patch.status in ("accepted", "rejected"))
        arrow = "▾" if self.archive_expanded else "▸"
        self.archive_toggle_btn.setText(f"{arrow} Archive ({archived_count})")
        for i in range(self.archive_container.count()):
            item = self.archive_container.itemAt(i)
            if item and item.widget():
                item.widget().setVisible(self.archive_expanded)

    def _on_decided(self, patch, approved):
        path = patch.path
        self.decisions_by_path.setdefault(path, []).append({"patch": patch, "approved": approved})
        for card in self.cards:
            if card.patch is patch:
                self.pending_container.removeWidget(card)
                self.archive_container.insertWidget(0, card)
                card.setVisible(self.archive_expanded)
                break
        self._refresh_archive_visibility()
        expected = self.expected_by_path.get(path, 0)
        if expected > 0 and len(self.decisions_by_path[path]) >= expected:
            to_emit = self.decisions_by_path.pop(path)
            self.expected_by_path.pop(path, None)
            self.all_decided.emit(to_emit)
