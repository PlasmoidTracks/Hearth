"""
Command execution approval dialog.

Shows the agent's requested shell command along with its self-provided
description, and lets the user Accept or Deny the command.

The dialog parses the full command string and displays each individual
command in the chain (splitting on &&, ||, ;, |), showing which ones are
already whitelisted and providing per-command allow/revoke buttons.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QPushButton, QLineEdit, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont


class _CmdActionBtn(QPushButton):
    """Per-command whitelist/revoke button stored in a table cell."""
    def __init__(self, command_name: str, is_whitelisted: bool, parent=None):
        super().__init__(parent)
        self._command_name = command_name
        self._whitelisted = is_whitelisted
        self._refresh()
        self.clicked.connect(self._on_click)
    
    def _refresh(self):
        if self._whitelisted:
            self.setText("Revoke")
            self.setStyleSheet("""
                QPushButton {
                    background:#5a2626; color:white; padding:3px 10px;
                    border-radius:4px; font-size:10px;
                }
                QPushButton:hover { background:#7a3030; }
            """)
        else:
            self.setText("Whitelist")
            self.setStyleSheet("""
                QPushButton {
                    background:#256b30; color:white; padding:3px 10px;
                    border-radius:4px; font-size:10px;
                }
                QPushButton:hover { background:#2d8a3e; }
            """)
    
    @property
    def command_name(self) -> str:
        return self._command_name
    
    def _on_click(self):
        self._whitelisted = not self._whitelisted
        self._refresh()


class CommandApprovalDialog(QDialog):
    """Modal dialog requesting approval for a run_command tool call.
    
    Returns via the accepted() signal when the user has made a decision.
    """
    accepted = Signal(bool, str)       # (approved, command_name_or_empty)
    denied = Signal(str)               # deny_reason

    def __init__(self, command: str, description: str, timeout: int = 30,
                 parent=None, all_commands: list = None,
                 allowlist: list = None):
        super().__init__(parent)
        self.setWindowTitle("Approve Command Execution")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setMinimumHeight(480)
        self._command = command
        self._description = description
        self._timeout = timeout
        self._deny_reason = ""
        self._all_commands = all_commands or []
        # Track allowlist state locally so buttons can toggle during the dialog
        self._local_allowlist = [a.get("command", "").lower() for a in (allowlist or [])]

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # --- Header ---
        header = QLabel("The agent wants to execute a shell command:")
        header.setStyleSheet("font-weight:bold; font-size:13px; color:#e6e6e6;")
        layout.addWidget(header)

        # --- Full command display (scrollable monospace box) ---
        cmd_frame = QFrame()
        cmd_frame.setFrameShape(QFrame.StyledPanel)
        cmd_frame.setStyleSheet(
            "QFrame { background:#1e1e1e; border:1px solid #444; border-radius:6px; }"
        )
        cmd_layout = QVBoxLayout(cmd_frame)
        cmd_label = QLabel("Full command:")
        cmd_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
        cmd_layout.addWidget(cmd_label)
        cmd_edit = QTextEdit()
        cmd_edit.setReadOnly(True)
        cmd_edit.setFont(QFont("Monospace", 10))
        cmd_edit.setPlainText(command)
        cmd_edit.setStyleSheet(
            "QTextEdit { background:#121212; color:#5591c7; border:none; font-family:monospace; }"
        )
        cmd_edit.setMinimumHeight(45)  # Start with ~2 lines visible
        # Wrap in a scroll area so the command text can grow but the dialog stays manageable
        cmd_scroll = QScrollArea()
        cmd_scroll.setWidget(cmd_edit)
        cmd_scroll.setWidgetResizable(True)
        cmd_scroll.setFrameShape(QFrame.NoFrame)
        cmd_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        cmd_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        cmd_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        cmd_layout.addWidget(cmd_scroll)
        timeout_label = QLabel(f"Timeout: {timeout}s  |  Shell: {self._shell()}")
        timeout_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
        cmd_layout.addWidget(timeout_label)
        layout.addWidget(cmd_frame)

        # --- Parsed commands table ---
        cmd_table_frame = QFrame()
        cmd_table_frame.setFrameShape(QFrame.StyledPanel)
        cmd_table_frame.setStyleSheet(
            "QFrame { background:#1a1a24; border:1px solid #3a3a4a; border-radius:6px; }"
        )
        cmd_table_layout = QVBoxLayout(cmd_table_frame)
        
        cmd_table_label = QLabel("Parsed commands:")
        cmd_table_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
        cmd_table_layout.addWidget(cmd_table_label)
        
        if self._all_commands:
            self.cmd_table = QTableWidget(len(self._all_commands), 3)
            self.cmd_table.setHorizontalHeaderLabels(["#", "Command", "Action"])
            header = self.cmd_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.Fixed)
            header.setSectionResizeMode(1, QHeaderView.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.cmd_table.setColumnWidth(0, 35)
            self.cmd_table.verticalHeader().setVisible(False)
            self.cmd_table.setShowGrid(True)
            self.cmd_table.setAlternatingRowColors(True)
            self.cmd_table.horizontalHeader().setHighlightSections(False)
            self.cmd_table.setItemPrototype(QTableWidgetItem())
            
            # Populate the table
            for i, cmd in enumerate(self._all_commands):
                # Row number
                num_item = QTableWidgetItem(str(i + 1))
                num_item.setTextAlignment(Qt.AlignCenter)
                num_item.setForeground(Qt.GlobalColor.darkGray)
                self.cmd_table.setItem(i, 0, num_item)
                
                # Command name
                cmd_item = QTableWidgetItem(cmd)
                cmd_item.setFont(QFont("Monospace", 10))
                self.cmd_table.setItem(i, 1, cmd_item)
                
                # Whitelist/Revoke button
                is_whitelisted = cmd.lower() in self._local_allowlist
                btn = _CmdActionBtn(cmd, is_whitelisted)
                self.cmd_table.setCellWidget(i, 2, btn)
            
            self.cmd_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            cmd_table_layout.addWidget(self.cmd_table)
        else:
            empty_label = QLabel("(no commands parsed — the command may contain only operators or whitespace)")
            empty_label.setStyleSheet("color:#8a8a8a; font-size:11px; font-style:italic;")
            cmd_table_layout.addWidget(empty_label)
        
        layout.addWidget(cmd_table_frame)

        # --- Description (fixed height, no scrolling) ---
        desc_frame = QFrame()
        desc_frame.setFrameShape(QFrame.StyledPanel)
        desc_frame.setStyleSheet(
            "QFrame { background:#1a1a24; border:1px solid #3a3a4a; border-radius:6px; }"
        )
        desc_layout = QVBoxLayout(desc_frame)
        desc_label = QLabel("Agent's description:")
        desc_label.setStyleSheet("color:#d19900; font-size:11px;")
        desc_layout.addWidget(desc_label)
        desc_edit = QTextEdit()
        desc_edit.setReadOnly(True)
        desc_edit.setFont(QFont("Sans-serif", 9))
        desc_edit.setPlainText(description if description else "(no description provided — the agent forgot to explain)")
        desc_edit.setStyleSheet(
            "QTextEdit { background:#12121a; color:#cdeecb; border:none; }"
        )
        desc_edit.setMaximumHeight(80)
        desc_edit.setMaximumWidth(500)
        # Disable scrolling entirely — just clamp to fixed height
        desc_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        desc_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        desc_layout.addWidget(desc_edit)
        layout.addWidget(desc_frame)

        # --- Warning ---
        warn = QLabel(
            "<i>Note: The description is self-provided by the agent.  If you are unsure, "
            "you can always deny the command and ask the agent to explain.</i>"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#8a8a8a; font-size:10px;")
        layout.addWidget(warn)

        # --- Deny reason ---
        self.deny_label = QLabel("Deny reason (optional):")
        self.deny_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
        self.deny_edit = QLineEdit()
        self.deny_edit.setPlaceholderText("e.g. 'looks destructive'")
        self.deny_edit.setVisible(True)
        layout.addWidget(self.deny_label)
        layout.addWidget(self.deny_edit)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.deny_btn = QPushButton("Deny")
        self.deny_btn.setStyleSheet(
            "QPushButton { background:#5a2626; color:white; padding:6px 16px; border-radius:4px; }"
            "QPushButton:hover { background:#7a3030; }"
        )
        self.deny_btn.clicked.connect(self._on_deny)
        btn_layout.addWidget(self.deny_btn)

        self.allow_btn = QPushButton("Allow")
        self.allow_btn.setStyleSheet(
            "QPushButton { background:#256b30; color:white; padding:6px 16px; border-radius:4px; }"
            "QPushButton:hover { background:#2d8a3e; }"
        )
        self.allow_btn.clicked.connect(self._on_allow)
        btn_layout.addWidget(self.allow_btn)

        layout.addLayout(btn_layout)

    def _shell(self) -> str:
        import sys
        if sys.platform == "win32":
            return "cmd /c"
        return "/bin/sh -c"

    def _on_allow(self):
        self.accepted.emit(True, "")
        self.accept()

    def _on_deny(self):
        self._deny_reason = self.deny_edit.text().strip()
        self.denied.emit(self._deny_reason)
        self.reject()

    def get_deny_reason(self) -> str:
        return self._deny_reason
    
    def get_local_allowlist(self) -> list:
        """Return the current local allowlist state (commands the user whitelisted during this dialog).

        Reads the live state of each Whitelist/Revoke button in the table so that
        commands the user whitelisted (or revoked) while the dialog was open are
        reflected in the returned list.
        """
        result = []
        if hasattr(self, "cmd_table") and self.cmd_table:
            for row in range(self.cmd_table.rowCount()):
                btn_widget = self.cmd_table.cellWidget(row, 2)
                if btn_widget is not None and getattr(btn_widget, "_whitelisted", False):
                    cmd_item = self.cmd_table.item(row, 1)
                    if cmd_item:
                        result.append(cmd_item.text().strip().lower())
        return result


# ── Quick self-test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv)
    
    # Test with a chained command (one whitelisted)
    dlg = CommandApprovalDialog(
        command='cd /tmp && rm -rf *',
        description="Clean up temp files",
        timeout=30,
        parent=None,
        all_commands=['cd', 'rm'],
        allowlist=[{"command": "cd", "description": "change dir"}],
    )
    dlg.exec()
    
    # Test with a piped command (none whitelisted)
    dlg2 = CommandApprovalDialog(
        command='grep foo bar | sort | uniq',
        description="Find and deduplicate",
        timeout=30,
        parent=None,
        all_commands=['grep', 'sort', 'uniq'],
        allowlist=[],
    )
    dlg2.exec()
