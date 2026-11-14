"""
Modal dialog shown when a headless browser needs to be killed before
a new web operation can proceed. Lets the user confirm or cancel.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
)
from PySide6.QtCore import Qt


class WebBrowserKillDialog(QDialog):
    """Prompt the user before killing an active headless browser.

    Returns one of:
        "kill"    — kill the browser and continue
        "cancel"  — skip this web operation (cancel the turn)
    """

    def __init__(self, browser_type: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kill Active Headless Browser")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._browser_type = browser_type
        self.choice = "kill"  # default
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        heading = QLabel(
            "<b>Active headless browser detected</b>"
        )
        heading.setStyleSheet("font-size: 14px;")
        layout.addWidget(heading)

        type_label = {
            "playwright": "Playwright",
            "selenium": "Selenium",
        }.get(self._browser_type, "Headless")

        info = QLabel(
            f"A <b>{type_label}</b> Chromium/Chrome instance is currently active "
            f"({self._browser_type} mode). The next web operation may hang or "
            f"fail if the existing browser is in a stuck or zombie state.\n\n"
            f"Kill it now so a fresh instance is started for the next operation?"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setMaximumHeight(100)
        details.setStyleSheet("background:#1e1e1e; color:#e6e6e6;")
        details.setPlainText(
            f"Browser type: {type_label}\n"
            f"Mode: {self._browser_type}\n"
            f"Action: Close active browser → Restart fresh → Retry operation"
        )
        layout.addWidget(details)

        note = QLabel(
            "The browser's cookies and session data are preserved. "
            "A new clean instance will be started automatically."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a8a8a;")
        layout.addWidget(note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        kill_btn = QPushButton("Kill browser & continue")
        kill_btn.setStyleSheet(
            "QPushButton { background: #5a3a2a; color: #e6e6e6; "
            "border: 1px solid #7a5a3a; border-radius: 4px; "
            "padding: 6px 12px; font-weight: bold; }"
            "QPushButton:hover { background: #7a5a3a; }"
        )
        kill_btn.clicked.connect(self._on_kill)
        btn_row.addWidget(kill_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def _on_kill(self):
        self.choice = "kill"
        self.accept()

    def _on_cancel(self):
        self.choice = "cancel"
        self.accept()
