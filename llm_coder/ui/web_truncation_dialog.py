"""
Modal dialog shown when a web fetch is truncated beyond configured limits.
Lets the user review the details and choose whether to proceed with the
truncated content or re-fetch with a higher limit.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
)
from PySide6.QtCore import Qt


class WebFetchTruncationDialog(QDialog):
    """Prompt the user when a fetch was truncated.

    Returns one of:
        "proceed" — accept the truncated content and continue
        "increase" — the agent should re-fetch with higher limits
        "cancel"  — cancel the current turn
    """

    def __init__(self, url: str, truncated_reasons: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Web Fetch Truncated")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._url = url
        self._truncated_reasons = truncated_reasons
        self.choice = "proceed"  # default
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        heading = QLabel(
            "<b>Web page content was truncated</b>"
        )
        heading.setStyleSheet("font-size: 14px;")
        layout.addWidget(heading)

        info = QLabel(
            f"The page at <b>{self._url}</b> exceeded one or more configured "
            "limits. Only part of the content was returned."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        reasons = QTextEdit()
        reasons.setReadOnly(True)
        reasons.setMaximumHeight(120)
        reasons.setStyleSheet("background:#1e1e1e; color:#e6e6e6;")
        reason_lines = [f"• {r}" for r in self._truncated_reasons]
        reasons.setPlainText("\n".join(reason_lines))
        layout.addWidget(reasons)

        note = QLabel(
            "You can increase the limits in <b>Settings → Web Access → "
            "Fetch Truncation</b> and try again, or proceed with the "
            "truncated content."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a8a8a;")
        layout.addWidget(note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        proceed_btn = QPushButton("Proceed with truncated content")
        proceed_btn.clicked.connect(self._on_proceed)
        btn_row.addWidget(proceed_btn)

        increase_btn = QPushButton("Increase limits & re-fetch")
        increase_btn.clicked.connect(self._on_increase)
        btn_row.addWidget(increase_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def _on_proceed(self):
        self.choice = "proceed"
        self.accept()

    def _on_increase(self):
        self.choice = "increase"
        self.accept()

    def _on_cancel(self):
        self.choice = "cancel"
        self.accept()
