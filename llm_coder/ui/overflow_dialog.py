"""
Modal choice dialog shown when context overflow policy is warn_and_ask.
Presents current usage and lets the user choose how to proceed.
"""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QPushButton


class OverflowChoiceDialog(QDialog):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Context limit reached")
        self.choice = "abort"
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            f"Estimated context usage: <b>{info['tokens']}</b> / {info['limit']} tokens.<br>"
            f"Auto-expand would raise the limit by {info['auto_expand_percent']}% "
            f"to approximately <b>{info['projected_limit']}</b> tokens."
        ))

        btn_row = QHBoxLayout()
        compact_btn = QPushButton("Compact history now")
        expand_btn = QPushButton(f"Expand by {info['auto_expand_percent']}%")
        continue_btn = QPushButton("Continue anyway (this once)")
        abort_btn = QPushButton("Abort turn")

        compact_btn.clicked.connect(lambda: self._pick("compact"))
        expand_btn.clicked.connect(lambda: self._pick("expand"))
        continue_btn.clicked.connect(lambda: self._pick("continue"))
        abort_btn.clicked.connect(lambda: self._pick("abort"))

        btn_row.addWidget(compact_btn)
        btn_row.addWidget(expand_btn)
        btn_row.addWidget(continue_btn)
        btn_row.addWidget(abort_btn)
        layout.addLayout(btn_row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()
