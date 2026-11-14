"""
Shown at startup if something is already responding at the configured
server URL. Lets the user attach to it or restart with a managed process.
"""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QPushButton


class ServerDetectDialog(QDialog):
    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server already running")
        self.choice = "attach"
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"A server already appears to be running at:<br><b>{base_url}</b><br><br>"
            "Would you like to use it as-is, or stop it and start a fresh managed instance?"
        ))
        btn_row = QHBoxLayout()
        attach_btn = QPushButton("Use existing server")
        restart_btn = QPushButton("Stop it and start fresh")
        ignore_btn = QPushButton("Ignore for now")
        attach_btn.clicked.connect(lambda: self._pick("attach"))
        restart_btn.clicked.connect(lambda: self._pick("restart"))
        ignore_btn.clicked.connect(lambda: self._pick("ignore"))
        btn_row.addWidget(attach_btn)
        btn_row.addWidget(restart_btn)
        btn_row.addWidget(ignore_btn)
        layout.addLayout(btn_row)

    def _pick(self, choice):
        self.choice = choice
        self.accept()
